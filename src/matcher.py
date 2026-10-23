import re
from dataclasses import asdict
from decimal import Decimal

from core.legacy_matching import (
    SPEC_TOL,
    _norm_company,
    direction_count,
    extract,
    missing_product_fields,
    score,
)
from core.matching.normalization import material_rank
from src.catalog_provider import LiveProvider


def _compatible(our, candidate, allow_missing=False):
    if our.get("속성충돌") or candidate.get("속성충돌"):
        return False
    supports, actual_supports = our.get("지지대수"), candidate.get("지지대수")
    if supports is None or (actual_supports != supports and (actual_supports is not None or not allow_missing)):
        return False
    left_dims, right_dims = our.get("규격"), candidate.get("규격")
    if not left_dims or (not right_dims and not allow_missing):
        return False
    if right_dims and any(o <= 0 or c <= 0 or abs(c-o) > o * Decimal(str(SPEC_TOL))
                          for o, c in zip(left_dims, right_dims)):
        return False
    if our.get("세부품명번호") and candidate.get("세부품명번호") != our["세부품명번호"]:
        return False
    if our.get("품명") and candidate.get("품명") != our["품명"]:
        return False
    for key in ("방향수", "표시면수"):
        expected = our.get(key)
        if key == "방향수" and expected is None:
            expected = direction_count(our.get("용도", ""))
        actual = candidate.get(key)
        if expected is not None and actual != expected and (actual is not None or not allow_missing):
            return False
    if our.get("형상") not in (None, "미상") and candidate.get("형상") != our["형상"] and (candidate.get("형상") != "미상" or not allow_missing):
        return False
    for key in ("설치방식", "발광방식", "반사방식"):
        expected = our.get("속성", {}).get(key)
        actual = candidate.get("속성", {}).get(key)
        if expected and actual != expected and (actual or not allow_missing):
            return False
    left, right = our.get("용도", ""), candidate.get("용도", "")
    if left:
        def normalized_use(value):
            return re.sub(r"\s+", "", re.sub(r"\d+\s*방향|단방향|일방향|양방향|두방향", "방향", value))
        if normalized_use(left) != normalized_use(right) and (right or not allow_missing):
            return False
    return True


def _preferred(our, candidate):
    left, right = our.get("규격"), candidate.get("규격")
    return bool(left and right and candidate["가격"] > (our.get("가격") or 0)
                and all(c <= o for o, c in zip(left, right)) and any(c < o for o, c in zip(left, right)))

def find_candidates(our, *, keyword=None, top_n=3, price_band=None, exact_price=False,
                    max_pool=500, dedupe_company=True, exclude_companies=None,
                    fallback_band=0.0, same_material=True, session=None, provider=None,
                    comparison=False):
    """우리 품목 our에 대한 유사 후보 top_n. 키워드 + 가격 필터로 풀 수집 후 유사도 순위.
    comparison=True: 규격·지지대 등 필수 검사 후 선정하고 가격 오름차순 배치.
    exact_price=True: 단가가 정확히 동일한 후보. 기본 비교는 기준보다 높은 가격.
    fallback_band>0: 동일단가 후보가 없으면 가격대를 넓혀 재시도. 필수 검사 유지.
    exclude_companies: 제외할 업체명 목록(정규화 exact 비교). 자기·계열사 매칭 방지."""
    if comparison and (our.get("지지대수") is None or not our.get("규격")):
        return []
    kw = keyword or our.get("품명", "")
    if comparison and not keyword and our.get("용도"):
        kw += " " + our["용도"]
    price = Decimal(str(our.get("가격") or 0))
    our_mat = our.get("재질원문") or our.get("재질", "")
    use_exact = exact_price and price > 0
    if use_exact:
        lo = hi = price                                 # 정확히 같은 단가만
    elif comparison and price and price_band is None:
        lo, hi = price, ""
    elif price:
        band = Decimal(str(price_band if price_band is not None else 0.5))
        lo, hi = price * (1 - band), price * (1 + band)
    else:
        lo = hi = ""
    excluded = {_norm_company(n) for n in (exclude_companies or [])}
    source = provider or LiveProvider()
    result = source.search(kw, str(lo), str(hi), max_pool)
    candidate_metadata = dict(asdict(result.metadata), total=result.total, truncated=result.truncated) if result.metadata else None

    scored = []
    for r in result.rows:
        c = extract(r)
        amount = c["가격"]
        if amount is None or amount <= 0:
            continue
        if c["식별번호"] == our.get("식별번호") and c["식별번호"]:
            continue
        if _norm_company(c.get("업체")) in excluded:
            continue
        if candidate_metadata is not None:
            c["catalog_metadata"] = candidate_metadata
        if (lo and amount < lo) or (hi and amount > hi):
            continue
        if comparison and not use_exact and amount <= price:
            continue
        if comparison and not _compatible(our, c, allow_missing=True):
            continue
        c["점수"], c["점수상세"] = score(our, c)
        scored.append(c)
    scored.sort(key=lambda x: -x["점수"])
    if comparison:
        enriched = []
        required = tuple(key for key in ("방향수", "표시면수", "지지대수") if our.get(key) is not None)
        required += tuple(key for key in ("설치방식", "발광방식", "반사방식") if our.get("속성", {}).get(key))
        detail_count = 0
        for candidate in scored:
            c = candidate
            if missing_product_fields(candidate, required) and not candidate["_raw"].get("_detail_checked_at"):
                if detail_count >= max(top_n * 8, 24):
                    continue
                detail_count += 1
                c = extract(source.enrich(candidate["_raw"], required=required))
            if candidate_metadata is not None:
                c["catalog_metadata"] = candidate_metadata
            if not _compatible(our, c):
                continue
            c["점수"], c["점수상세"] = score(our, c)
            c["규격가격우선"] = _preferred(our, c)
            rank = material_rank(our_mat, c.get("재질원문") or c.get("재질", ""))
            c["매칭근거"] = "동일 재질 명칭" if rank == 3 else "재질 계열 일치·세부 사양 확인" if rank == 2 else "혼합 재질 포함" if rank == 1 else "재질 미확인"
            c["매칭근거"] += f" / 지지대 {c['지지대수']}개 일치 / 규격 각 변 ±{SPEC_TOL:.0%} 이내"
            if not c["규격가격우선"]:
                c["매칭근거"] += " / 작은 규격·높은 가격 조건 미충족"
            if same_material and our_mat and not rank:
                continue
            enriched.append(c)
        scored = sorted(enriched, key=lambda c: (-material_rank(our_mat, c.get("재질원문") or c.get("재질", "")),
                                                -c["규격가격우선"], -c["점수"], c["가격"] or Decimal(0), str(c["식별번호"])))

    top, seen = [], set()
    for c in scored:
        comp = c.get("업체")
        if not comp:
            continue
        if same_material and our_mat and not material_rank(our_mat, c.get("재질원문") or c.get("재질", "")):
            continue
        company_key = _norm_company(comp)
        if company_key in excluded:                    # 자기·계열사 제외 (정규화 exact)
            continue
        if dedupe_company and company_key in seen:
            continue
        seen.add(company_key)
        top.append(c)
        if len(top) >= top_n:
            break

    if not top and use_exact and fallback_band > 0:      # 동일단가 후보 없음 → 근사(가격대) 폴백
        approx = find_candidates(our, keyword=keyword, top_n=top_n, price_band=fallback_band,
                                 exact_price=False, max_pool=max_pool, dedupe_company=dedupe_company,
                                 exclude_companies=exclude_companies, same_material=same_material,
                                 session=session, provider=provider, comparison=comparison)
        for c in approx:
            c["근사"] = True
        return approx
    return sorted(top, key=lambda c: (c["가격"] or Decimal(0), str(c["식별번호"]))) if comparison else top
