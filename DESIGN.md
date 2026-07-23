# 설계: 나라장터 가격비교 자동화 (FastAPI + MySQL)

## 가정 (brainstorm 열린 질문 기본값)
- 단가 비교 기준 = **나라장터 등록단가끼리** (스샷 비교표 구조와 동일)
- 이미지 = **스토리지 B** (우리 스토리지 저장, 우리 URL 서빙; 로컬 → S3 스왑 가능)
- 매칭 재실행 = **run_id 이력** 저장 (단가 변동 추적)
- 우리 품목 = 전 품목 대상 (일부만이면 필터 추가)

## 아키텍처

```
[우리 DB: our_product]
   │  POST /match { product_id, top_n }
   ▼
FastAPI  app.py
   │  1) our_product 로드
   │  2) run_in_threadpool → g2b_client(크롤) + matcher(유사후보 top-N)   ← 동기, 스레드풀
   │  3) httpx.AsyncClient + Semaphore → 후보 이미지 동시 다운로드         ← 비동기 I/O
   │  4) 스토리지 B 저장 → 우리 URL
   ▼
[우리 DB: competitor_match(1:N) + product_image]  ──▶  응답(JSON) / 엑셀(build_report)
```

핵심: **크롤·매칭은 동기(requests)라 스레드풀로 격리, 이미지는 async(httpx)** — 하이브리드가
표준. 이미지가 I/O 바운드라 async 이득이 가장 큰 구간(실측 20장/동시8 ≈ 0.2초, 100%).

## 데이터 모델 (→ `schema.sql`)
- `our_product` — **PK=사내 품번(product_id)**. 물품식별번호는 nullable 속성(등록분만).
  매칭 구동 컬럼: name·dtls_prnm_no·spec_w/h/t·material·usage_desc·model·our_price
- `competitor_match` — 우리품목 1:N 경쟁사item. run_id로 재실행 이력. 점수/순위/경쟁사 스펙·단가·계약구분
- `product_image` — 스토리지 B. match_id FK, storage_key·our_url·bytes

키 필드 판단 근거: 우리 제품이 나라장터 미등록일 수 있어 물품식별번호는 PK 부적합 → 사내 품번 surrogate PK.

## API 계약

```
POST /match
  req : { "product_id": "GK-BF02", "top_n": 3, "price_band": 0.5, "keyword": "점자 안내판" }
  resp: { "run_id": "...", "product_id": "GK-BF02",
          "candidates": [ { "rank":1, "score":0.75, "company":"주식회사 아트포시즌",
                            "model":"AT4-BF02", "spec":[500,450], "material":"스테인리스",
                            "usage":"", "price":1300000, "contract_type":"다수공급자계약",
                            "image_url":"/static/images/<key>.jpg" }, ... ] }
  동작: our_product 조회 → 매칭 → 이미지 async 수집(스토리지 B) → competitor_match/product_image upsert
```

향후: `GET /products/{id}/matches`(조회), `POST /report`(엑셀 생성, build_report 재사용).

## 파일 구성
```
app.py            FastAPI /match (크롤+매칭+async이미지+DB upsert)
schema.sql        MySQL DDL (3 테이블)
g2b_client.py     나라장터 크롤 코어 (재사용)
matcher.py        유사후보 매칭 (재사용)
build_report.py   비교표 엑셀 생성 (재사용)
```

## 실행 의존성
```
pip install fastapi uvicorn httpx sqlalchemy aiomysql   # API 계층
# 크롤/매칭/엑셀 계층은 requests + openpyxl + pillow (기존)
```

## 다음 단계
`/sc:implement` — MySQL 연결 실환경에서 /match 엔드투엔드 구동 + 시드 데이터 적재.
