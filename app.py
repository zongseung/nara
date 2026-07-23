"""
FastAPI + MySQL  /match 엔드포인트 (스캐폴드).

우리 품목(product_id) → 나라장터 유사후보 매칭 → 이미지 async 수집(스토리지 B) → 우리 DB upsert.
동기 크롤/매칭(g2b_client, matcher)은 run_in_threadpool로, 이미지는 httpx async로.

실행:
    pip install fastapi uvicorn httpx sqlalchemy aiomysql
    mysql < schema.sql
    DB_URL="mysql+aiomysql://user:pw@localhost/g2b" uvicorn app:app --reload
"""
import asyncio
import hashlib
import os
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import matcher

DB_URL = os.getenv("DB_URL", "mysql+aiomysql://user:pw@localhost/g2b")
STORAGE_DIR = os.getenv("STORAGE_DIR", "./storage/images")   # 스토리지 B(로컬; S3로 스왑 가능)
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "/static/images")     # 우리가 서빙하는 URL prefix
NARA = "https://shop.g2b.go.kr"

engine = create_async_engine(DB_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI(title="g2b price-match")
os.makedirs(STORAGE_DIR, exist_ok=True)
app.mount(PUBLIC_BASE, StaticFiles(directory=STORAGE_DIR), name="images")


class MatchReq(BaseModel):
    product_id: str
    top_n: int = 3
    price_band: float = 0.5
    keyword: str | None = None


async def _store_image(cli, img_src):
    """nara 이미지 async 다운로드 → 스토리지 B 저장 → (our_url, key, bytes). 실패 시 None.
    ponytail: 로컬 파일 저장. S3면 이 함수 본문만 put_object로 교체(인터페이스 동일)."""
    if not img_src:
        return None
    url = img_src if img_src.startswith("http") else NARA + img_src
    try:
        r = await cli.get(url, timeout=30)
        r.raise_for_status()
        key = hashlib.sha1(url.encode()).hexdigest() + ".jpg"
        with open(os.path.join(STORAGE_DIR, key), "wb") as f:
            f.write(r.content)
        return f"{PUBLIC_BASE}/{key}", key, len(r.content)
    except Exception:
        return None


async def _collect_images(cands, conc=8):
    """후보 이미지들을 동시 conc개로 async 수집. 실측: 20장/동시8 ≈ 0.2초."""
    sem = asyncio.Semaphore(conc)
    async with httpx.AsyncClient() as cli:
        async def one(c):
            async with sem:
                return await _store_image(cli, c.get("imgSrc"))
        return await asyncio.gather(*[one(c) for c in cands])


@app.post("/match")
async def match(req: MatchReq):
    # 1) 우리 품목 로드
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT product_id,name,dtls_prnm_no,spec_w,spec_h,spec_t,material,usage_desc,model,our_price"
            " FROM our_product WHERE product_id=:pid"), {"pid": req.product_id})).mappings().first()
    if not row:
        raise HTTPException(404, "product not found")

    our = {"품명": row["name"], "규격": (row["spec_w"], row["spec_h"]),
           "재질": row["material"], "용도": row["usage_desc"],
           "가격": row["our_price"], "모델": row["model"], "식별번호": None}

    # 2) 동기 크롤/매칭은 스레드풀에서 (이벤트루프 안 막음)
    cands = await run_in_threadpool(
        matcher.find_candidates, our,
        keyword=req.keyword or row["name"], top_n=req.top_n, price_band=req.price_band)

    # 3) 이미지 async 수집 → 스토리지 B
    images = await _collect_images(cands)

    # 4) 우리 DB upsert (매칭 + 이미지)
    run_id = str(uuid.uuid4())
    async with Session() as s:
        async with s.begin():
            for i, (c, img) in enumerate(zip(cands, images), 1):
                w, h = (c.get("규격") or (None, None))
                res = await s.execute(text(
                    "INSERT INTO competitor_match(product_id,run_id,rank_no,score,comp_company,"
                    "comp_item_idnf,comp_model,comp_spec_w,comp_spec_h,comp_material,comp_usage,"
                    "comp_price,contract_type,src_img_url) VALUES"
                    "(:pid,:run,:rk,:sc,:co,:idnf,:mo,:w,:h,:mat,:use,:pr,:ct,:img)"),
                    {"pid": req.product_id, "run": run_id, "rk": i, "sc": c["점수"],
                     "co": c["업체"], "idnf": c["식별번호"], "mo": c["모델"], "w": w, "h": h,
                     "mat": c["재질"], "use": c["용도"], "pr": c["가격"],
                     "ct": c.get("계약구분"),
                     "img": (NARA + c["imgSrc"]) if c.get("imgSrc") else None})
                if img:
                    our_url, key, nbytes = img
                    await s.execute(text(  # lastrowid: MySQL·SQLite 공통 이식성
                        "INSERT INTO product_image(match_id,storage_key,our_url,bytes)"
                        " VALUES(:mid,:k,:u,:b)"),
                        {"mid": res.lastrowid, "k": key, "u": our_url, "b": nbytes})

    return {"run_id": run_id, "product_id": req.product_id,
            "candidates": [
                {"rank": i, "score": c["점수"], "company": c["업체"], "model": c["모델"],
                 "spec": c["규격"], "material": c["재질"], "usage": c["용도"],
                 "price": c["가격"], "contract_type": c.get("계약구분"),
                 "image_url": (img[0] if img else None)}
                for i, (c, img) in enumerate(zip(cands, images), 1)]}
