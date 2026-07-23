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

# 재질 정규화: 세부 표기(적삼목/스텐/알미늄 등)를 대분류로 묶음 (재질이 1순위라 매칭 견고성 중요)
_MAT_SYNONYMS = {
    "스테인리스": ["스테인리스", "스테인레스", "스텐", "sus", "sts"],
    "목재": ["목재", "나무", "원목", "적삼목", "낙엽송", "삼나무", "방부목", "미송", "합판", "집성목", "레드파인", "홍송", "루버"],
    "알루미늄": ["알루미늄", "알미늄", "알류미늄"],
    "아크릴": ["아크릴"],
    "포맥스": ["포맥스", "폼보드"],
    "동판": ["동판", "구리", "황동", "브론즈"],
    "플라스틱": ["플라스틱", "abs", "pvc"],
    "철": ["철판", "스틸", "steel", "강판", "아연도", "철"],
}
# 재질이 절대 1순위(동일 재질이 항상 상위) · 규격은 비슷하면 OK(느슨·저비중) · 가격은 동일단가 하드필터
WEIGHTS = {"재질": 0.55, "용도": 0.20, "규격": 0.10, "가격": 0.15}
SPEC_TOL = 0.35   # 규격 상대차 ±35% 이내면 '비슷'으로 보고 만점


def norm_material(s):
    """세부 재질 표기 → 대분류. (적삼목→목재, 스텐/SUS→스테인리스 등)"""
    s = (s or "").lower()
    for cat, syns in _MAT_SYNONYMS.items():
        if any(syn in s for syn in syns):
            return cat
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
    # 재질: 정규화 후 동일 여부 (양쪽 다 norm_material). 1순위라 이게 순위를 지배.
    our_mat = norm_material(our.get("재질"))
    p["재질"] = 1.0 if our_mat and our_mat == cand.get("재질") else 0.0
    # 규격: 비슷하기만 하면 됨 — ±SPEC_TOL 이내는 만점, 그 밖은 완만히 감점
    ov, cv = our.get("규격"), cand.get("규격")
    if ov and cv and ov[0] and ov[1]:
        d = (abs(ov[0] - cv[0]) / ov[0] + abs(ov[1] - cv[1]) / ov[1]) / 2
        p["규격"] = 1.0 if d <= SPEC_TOL else max(0.0, 1 - (d - SPEC_TOL))
    else:
        p["규격"] = 0.7
    p["용도"] = difflib.SequenceMatcher(None, our.get("용도", ""), cand.get("용도", "")).ratio()
    if our.get("가격"):
        p["가격"] = max(0.0, 1 - abs(our["가격"] - cand["가격"]) / our["가격"])
    else:
        p["가격"] = 0.5
    return sum(WEIGHTS[k] * p[k] for k in WEIGHTS), p


def find_candidates(our, *, keyword=None, top_n=3, price_band=0.5, exact_price=False,
                    max_pool=500, dedupe_company=True, exclude_company=None, session=None):
    """우리 품목 our에 대한 유사 후보 top_n. 키워드 + 가격 필터로 풀 수집 후 유사도 순위.
    exact_price=True: 단가가 '정확히 동일'한 것만(제안팀 비교표 — 같은 단가에서 규격/품질로 경쟁).
    exact_price=False: ±price_band 가격대.
    exclude_company: 우리 업체명(부분일치)은 후보에서 제외(자기 자신 매칭 방지)."""
    kw = keyword or our.get("품명", "")
    price = our.get("가격") or 0
    if not price:
        lo = hi = ""
    elif exact_price:
        lo = hi = price                                 # 정확히 같은 단가만
    else:
        lo, hi = int(price * (1 - price_band)), int(price * (1 + price_band))
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
