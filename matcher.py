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
    "강화유리": ["강화유리", "유리"],
    "갈바": ["갈바", "갈바늄", "갈바륨"],
    "포맥스": ["포맥스", "폼보드"],
    "동판": ["동판", "구리", "황동", "브론즈"],
    "플라스틱": ["플라스틱", "abs", "pvc"],
    "철": ["철판", "스틸", "steel", "강판", "강재", "아연도", "철"],  # 압연강재/내후성강재 포함
}


def _to_price(v):
    """단가 안전 파싱 → 정수. 이상값(NaN·문자·None)은 0."""
    try:
        n = int(float(v))
        return n if n >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _norm_company(name):
    """업체명 정규화: 법인격·공백 제거 후 소문자. exact 집합 비교용(substring 오제외 방지)."""
    s = str(name or "")
    for t in ("주식회사", "(주)", "㈜", "유한회사", "(유)", "(재)", "(사)"):
        s = s.replace(t, "")
    return "".join(s.split()).lower()
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
        "업체": html.unescape(raw.get("mnftrEtpsNm") or raw.get("ctentUntyGrpNm") or ""),
        "식별번호": raw.get("itemIdnfNo"),
        "품명": name.split(",")[0].strip() if name else "",   # itemIdnfNm 첫 토큰(안내판/간판)
        "세부품명": raw.get("dtlsPrnm"),
        "세부품명번호": raw.get("dtlsPrnmNo"),
        "이름": name,
        "규격": parse_dims(attrs) or parse_dims(name),
        "재질": norm_material(attrs + " " + name),
        "용도": parse_use(attrs),
        "가격": _to_price(raw.get("ctrtUprc")),
        "모델": _model(name),
        "imgSrc": raw.get("imgSrc"),
        "계약구분": raw.get("shopCtrtTyNm"),
        "_raw": raw,
    }


def score(our, cand):
    """(총점, 항목별점수). our/cand 모두 extract 형식(우리 품목은 손으로 채운 dict)."""
    p = {}
    # 재질: 양쪽 다 norm_material 로 정규화 후 동일 여부 (1순위 — 순위를 지배).
    our_mat, cand_mat = norm_material(our.get("재질")), norm_material(cand.get("재질"))
    p["재질"] = 1.0 if our_mat and our_mat == cand_mat else 0.0
    # 규격: 비슷하기만 하면 됨 — 두 변 각각의 상대차 중 큰 쪽(max)이 ±SPEC_TOL 이내면 만점.
    ov, cv = our.get("규격"), cand.get("규격")
    if ov and cv and ov[0] and ov[1]:
        d = max(abs(ov[0] - cv[0]) / ov[0], abs(ov[1] - cv[1]) / ov[1])
        p["규격"] = 1.0 if d <= SPEC_TOL else max(0.0, 1 - (d - SPEC_TOL))
    else:
        p["규격"] = 0.7
    p["용도"] = difflib.SequenceMatcher(None, our.get("용도", ""), cand.get("용도", "")).ratio()
    op, cp = _to_price(our.get("가격")), _to_price(cand.get("가격"))
    p["가격"] = max(0.0, 1 - abs(op - cp) / op) if op else 0.5
    return sum(WEIGHTS[k] * p[k] for k in WEIGHTS), p


def find_candidates(our, *, keyword=None, top_n=3, price_band=0.5, exact_price=False,
                    max_pool=500, dedupe_company=True, exclude_companies=None,
                    fallback_band=0.0, session=None):
    """우리 품목 our에 대한 유사 후보 top_n. 키워드 + 가격 필터로 풀 수집 후 유사도 순위.
    exact_price=True: 단가가 '정확히 동일'한 것만(제안팀 비교표 — 같은 단가에서 규격/품질로 경쟁).
    exact_price=False: ±price_band 가격대.
    fallback_band>0: 동일단가 후보가 0이면 ±fallback_band 가격대로 재시도(근사, 각 후보에 근사=True).
    exclude_companies: 제외할 업체명 목록(정규화 exact 비교). 자기·계열사 매칭 방지."""
    kw = keyword or our.get("품명", "")
    price = _to_price(our.get("가격"))
    use_exact = exact_price and price > 0
    if use_exact:
        lo = hi = price                                 # 정확히 같은 단가만
    elif price:
        lo, hi = int(price * (1 - price_band)), int(price * (1 + price_band))
    else:
        lo = hi = ""
    s = session or requests.Session()
    excluded = {_norm_company(n) for n in (exclude_companies or [])}

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
        if use_exact and c["가격"] != price:            # API 신뢰 안 하고 로컬에서도 동일단가 강제
            continue
        c["점수"], c["점수상세"] = score(our, c)
        scored.append(c)
    scored.sort(key=lambda x: -x["점수"])

    top, seen = [], set()
    for c in scored:
        comp = c.get("업체")
        if not comp:
            continue
        if _norm_company(comp) in excluded:             # 자기·계열사 제외 (정규화 exact)
            continue
        if dedupe_company and comp in seen:
            continue
        seen.add(comp)
        top.append(c)
        if len(top) >= top_n:
            break

    if not top and use_exact and fallback_band > 0:      # 동일단가 후보 없음 → 근사(가격대) 폴백
        approx = find_candidates(our, keyword=keyword, top_n=top_n, price_band=fallback_band,
                                 exact_price=False, max_pool=max_pool, dedupe_company=dedupe_company,
                                 exclude_companies=exclude_companies, session=s)
        for c in approx:
            c["근사"] = True
        return approx
    return top
