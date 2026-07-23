"""
우리 품목 1건 → 나라장터 종합쇼핑몰에서 '유사 후보' top-N 추출.

유사도 = 재질(0.35) + 규격근접(0.25) + 용도유사(0.25) + 가격근접(0.15) 가중합.
후보 풀은 '같은 가격대(±price_band) + 키워드'로 좁혀서 가져온다(사람이 같은 가격대끼리
비교하던 방식과 동일). 점수는 투명한 규칙 기반 — 나중에 규칙만 손보면 됨.

ponytail: 재질/용도/규격 파싱은 pdctAtrbCdDtlNm 문자열 휴리스틱. 표기가 특이한 품목은
          놓칠 수 있음 → 후보를 '추려주는' 용도이고 최종 확정은 사람이 검수(반자동).
"""
import difflib
import html
import re

import requests
import g2b_client as g2b

MATERIALS = ["스테인리스", "알루미늄", "아크릴", "포맥스", "동판", "황동",
             "목재", "플라스틱", "금속", "철판", "철"]
WEIGHTS = {"재질": 0.35, "규격": 0.25, "용도": 0.25, "가격": 0.15}


def norm_material(s):
    """재질 문자열 정규화. '스테인리스강'/'SUS' 등 → '스테인리스'."""
    s = s or ""
    if "스테인" in s or "SUS" in s.upper():
        return "스테인리스"
    for m in MATERIALS:
        if m in s:
            return m
    return ""


def parse_dims(s):
    """'500×450' / '500x450mm' → (500, 450). 없으면 None."""
    m = re.search(r"(\d+)\s*[×xX*]\s*(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_use(s):
    """'... 용도:도로명판걸이' → '도로명판걸이'."""
    m = re.search(r"용도\s*[:：]\s*([^,|]+)", s or "")
    return m.group(1).strip() if m else ""


def _model(name):
    """이름 토큰 중 영문+숫자 섞인 걸 모델명 후보로."""
    for tok in re.split(r"[,\s]+", name or ""):
        if re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok):
            return tok.strip("()")
    return ""


def extract(raw):
    """API 원본 row → 매칭용 dict (원본은 _raw로 보관)."""
    attrs = raw.get("pdctAtrbCdDtlNm", "") or ""
    name = html.unescape(raw.get("itemIdnfNm", "") or "")
    return {
        "업체": raw.get("mnftrEtpsNm") or raw.get("ctentUntyGrpNm"),
        "식별번호": raw.get("itemIdnfNo"),
        "품명": name.split(",")[0].strip() if name else "",   # itemIdnfNm 첫 토큰(안내판/간판)
        "세부품명": raw.get("dtlsPrnm"),
        "세부품명번호": raw.get("dtlsPrnmNo"),
        "이름": name,
        "규격": parse_dims(attrs) or parse_dims(name),
        "재질": norm_material(attrs + " " + name),
        "용도": parse_use(attrs),
        "가격": int(float(raw.get("ctrtUprc") or 0)),
        "모델": _model(name),
        "imgSrc": raw.get("imgSrc"),
        "계약구분": raw.get("shopCtrtTyNm"),
        "_raw": raw,
    }


def score(our, cand):
    """(총점, 항목별점수). our/cand 모두 extract 형식(우리 품목은 손으로 채운 dict)."""
    p = {}
    p["재질"] = 1.0 if our.get("재질") and our["재질"] == cand.get("재질") else 0.0
    ov, cv = our.get("규격"), cand.get("규격")
    if ov and cv and ov[0] and ov[1]:
        d = (abs(ov[0] - cv[0]) / ov[0] + abs(ov[1] - cv[1]) / ov[1]) / 2
        p["규격"] = max(0.0, 1 - d)
    else:
        p["규격"] = 0.5
    p["용도"] = difflib.SequenceMatcher(None, our.get("용도", ""), cand.get("용도", "")).ratio()
    if our.get("가격"):
        p["가격"] = max(0.0, 1 - abs(our["가격"] - cand["가격"]) / our["가격"])
    else:
        p["가격"] = 0.5
    return sum(WEIGHTS[k] * p[k] for k in WEIGHTS), p


def find_candidates(our, *, keyword=None, top_n=3, price_band=0.5,
                    max_pool=500, dedupe_company=True, exclude_company=None, session=None):
    """우리 품목 our에 대한 유사 후보 top_n. 같은 가격대(±price_band)+키워드로 풀 수집.
    exclude_company: 우리 업체명(부분일치)은 후보에서 제외(자기 자신 매칭 방지)."""
    kw = keyword or our.get("품명", "")
    price = our.get("가격") or 0
    lo = int(price * (1 - price_band)) if price else ""
    hi = int(price * (1 + price_band)) if price else ""
    s = session or requests.Session()

    pool, page = [], 1
    while len(pool) < max_pool:
        raw, _ = g2b.fetch_page(page, keyword=kw, start_prce=lo, end_prce=hi, session=s)
        if not raw:
            break
        pool.extend(raw)
        if len(raw) < 100:
            break
        page += 1

    scored = []
    for r in pool:
        c = extract(r)
        c["점수"], c["점수상세"] = score(our, c)
        scored.append(c)
    scored.sort(key=lambda x: -x["점수"])

    top, seen = [], set()
    for c in scored:
        if exclude_company and c["업체"] and exclude_company in c["업체"]:
            continue                                    # 자기 업체 제외
        if dedupe_company and c["업체"] in seen:
            continue
        seen.add(c["업체"])
        top.append(c)
        if len(top) >= top_n:
            break
    return top
