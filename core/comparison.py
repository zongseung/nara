"""기준 품목 하나의 경쟁사 비교: 필수 구성 검사 → 부족한 속성만 상세 보강 → 재질·규격·가격 순 선정.

조회는 주입된 CandidateProvider만 거치므로 실시간·진단 DB·테스트 대역이 같은 규칙으로 판정된다."""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Final, Protocol

from core.catalog_sources import RawRow
from core.legacy_matching import (
    SPEC_TOL,
    ComparisonItem,
    direction_count,
    extract,
    missing_product_fields,
    norm_company,
    score,
)
from core.matching.normalization import material_profile, material_rank

MAX_POOL: Final = 500
# shortcut: 촬영용으로 지지대 검사를 중지함; 개수 확인 경로를 연결한 뒤 True로 복원.
CHECK_SUPPORTS = False
SOFT_FIELDS: Final = ("표시면수", "설치방식", "발광방식", "반사방식")
REVIEW_ORDER: Final = {"조건 충족": 0, "확인 필요": 1, "대안 후보": 2}


@dataclass(frozen=True, slots=True)
class RunMetadata:
    run_id: str
    scope: str
    profile_hash: str
    observed_from: str
    observed_to: str | None
    state: str
    validation: str
    incomplete: bool
    source_snapshot_verified: bool = False
    search_semantics: str = (
        "Unicode NFKC casefold whitespace-token AND substring; not server-equivalent"
    )


@dataclass(frozen=True, slots=True)
class CandidatePage:
    rows: tuple[RawRow, ...]
    total: int
    truncated: bool
    metadata: RunMetadata | None


class CandidateProvider(Protocol):
    def search(
        self, keyword: str, lower: str = "", upper: str = "", limit: int = 500
    ) -> CandidatePage: ...

    def enrich(self, raw: RawRow, *, required: tuple[str, ...] = ()) -> RawRow: ...


def compatible(our, candidate, allow_missing=False, *, same_direction=False):
    return not _exclusion_reason(our, candidate, allow_missing, same_direction=same_direction)


def _exclusion_reason(our, candidate, allow_missing=False, *, same_direction=False):
    conflicts = set(our.get("속성충돌", ())) | set(candidate.get("속성충돌", ()))
    conflicts.difference_update(SOFT_FIELDS)
    if not same_direction:
        conflicts.discard("방향수")
    if not CHECK_SUPPORTS:
        conflicts.discard("지지대수")
    if conflicts:
        return "속성 충돌"
    supports, actual_supports = our.get("지지대수"), candidate.get("지지대수")
    if CHECK_SUPPORTS and (supports is None or (actual_supports != supports and (actual_supports is not None or not allow_missing))):
        return "지지대"
    left_dims, right_dims = our.get("규격"), candidate.get("규격")
    if not left_dims or (not right_dims and not allow_missing):
        return "규격 미확인"
    if right_dims and any(o <= 0 or c <= 0 or abs(c-o) > o * Decimal(str(SPEC_TOL))
                          for o, c in zip(left_dims, right_dims)):
        return "규격 범위"
    for key in ("세부품명번호", "품명"):
        if our.get(key) and candidate.get(key) and candidate[key] != our[key]:
            return "품목 분류"
    for key in (("방향수",) if same_direction else ()):
        expected = our.get(key)
        if key == "방향수" and expected is None:
            expected = direction_count(our.get("용도", ""))
        actual = candidate.get(key)
        if expected is not None and actual != expected and (actual is not None or not allow_missing):
            return "방향수"
    if our.get("형상") not in (None, "", "미상") and candidate.get("형상") not in (None, "", "미상", our["형상"]):
        return "형상"
    return ""


def _review_status(our, candidate):
    missing, different = [], []
    for key in ("품명", "세부품명번호", "형상", "재질원문"):
        if our.get(key) in (None, "", "미상") or candidate.get(key) in (None, "", "미상"):
            missing.append(f"{key} 미확인")
    conflicts = set(our.get("속성충돌", ())) | set(candidate.get("속성충돌", ()))
    for key in SOFT_FIELDS:
        expected = our.get(key) if key == "표시면수" else our.get("속성", {}).get(key)
        actual = candidate.get(key) if key == "표시면수" else candidate.get("속성", {}).get(key)
        if key in conflicts:
            missing.append(f"{key} 정보 충돌")
        elif expected in (None, "") or actual in (None, ""):
            if expected not in (None, "") or actual not in (None, ""):
                missing.append(f"{key} 미확인")
        elif expected != actual:
            different.append(f"{key} 차이: {expected} → {actual}")
    return ("대안 후보" if different else "확인 필요" if missing else "조건 충족"), missing + different


def _preferred(our, candidate):
    left, right = our.get("규격"), candidate.get("규격")
    return bool(left and right and candidate["가격"] > (our.get("가격") or 0)
                and all(c <= o for o, c in zip(left, right)) and any(c < o for o, c in zip(left, right)))


def compare(our: ComparisonItem, provider: CandidateProvider, *, top_n: int = 3, exact_price: bool = False,
            fallback_band: float = 0.0, excluded: Iterable[str] = (),
            confirmed_supports: int | None = None,
            on_stage: Callable[[str], None] | None = None,
            same_direction: bool = False, excluded_ids: Iterable[str] = (),
            required_material: str = '') -> tuple[ComparisonItem, list[ComparisonItem]]:
    """(보강·보류 사유를 반영한 기준 품목, 가격 오름차순 경쟁사 최대 top_n).
    기준 규격이 미확인이면 비교를 보류한다. 지지대 검사는 CHECK_SUPPORTS로 임시 중지.
    exact_price=True: 같은 단가만. 하나도 없고 fallback_band>0이면 기준 초과~+band 가격대로 재검색(근사).
    excluded: 제외할 업체명(정규화 exact 비교). 자기·계열사 매칭 방지."""
    raw = our.get("_raw")
    if raw:
        enriched = provider.enrich(raw, required=("지지대수",) if CHECK_SUPPORTS else ())
        if enriched is not raw:
            our = {**our, **extract(enriched)}
    if confirmed_supports is not None:
        if type(confirmed_supports) is not int or confirmed_supports < 0:
            raise ValueError('invalid_confirmed_supports')
        if our.get('지지대수') is not None and our['지지대수'] != confirmed_supports:
            raise ValueError('confirmed_supports_conflict')
        our = {**our, '지지대수': confirmed_supports}
    our = our.copy()
    our.pop('비교보류사유', None)
    our.pop('후보진단', None)
    missing = [key for key in (("규격", "지지대수") if CHECK_SUPPORTS else ("규격",)) if our.get(key) is None]
    if missing:
        return {**our, "비교보류사유": "비교 보류: " + ", ".join(missing) + " 미확인"}, []
    blocked = {norm_company(name) for name in excluded}
    filters = {'excluded_ids': set(excluded_ids), 'required_material': required_material}
    price = Decimal(str(our.get("가격") or 0))
    if not (exact_price and price > 0):
        return our, _select(our, provider, price, price or "", "", top_n, blocked, on_stage=on_stage, same_direction=same_direction, **filters)
    found = _select(our, provider, price, price, price, top_n, blocked, exact=True, on_stage=on_stage, same_direction=same_direction, **filters)
    if found or fallback_band <= 0:
        return our, found
    band = Decimal(str(fallback_band))
    approx = _select(our, provider, price, price * (1 - band), price * (1 + band), top_n, blocked, on_stage=on_stage, same_direction=same_direction, **filters)
    marked: list[ComparisonItem] = [{**c, "근사": True} for c in approx]
    return our, marked


def _select(our, provider, price, lo, hi, top_n, blocked, *, exact=False, on_stage=None, same_direction=False,
            excluded_ids=(), required_material=''):
    keyword = our.get("품명", "")
    our_mat = our.get("재질원문") or our.get("재질", "")
    if on_stage:
        on_stage('searching')
    result = provider.search(keyword, str(lo), str(hi), MAX_POOL)
    counts = Counter({"검색결과": result.total, "검사후보": len(result.rows), "조회범위제한": int(result.truncated)})
    our["후보진단"] = counts
    metadata = dict(asdict(result.metadata), total=result.total, truncated=result.truncated) if result.metadata else None

    if on_stage:
        on_stage('checking')
    scored = []
    for row in result.rows:
        c = extract(row)
        if c['식별번호'] in excluded_ids or (excluded_ids and not c['식별번호']):
            counts["탈락: 제외 제품"] += 1
            continue
        reason = _price_or_identity_reason(our, c, price, lo, hi, exact, blocked) or _exclusion_reason(
            our, c, allow_missing=True, same_direction=same_direction)
        if reason:
            counts["탈락: " + reason] += 1
            continue
        if metadata is not None:
            c["catalog_metadata"] = metadata
        c["점수"], c["점수상세"] = score(our, c)
        scored.append(c)
    scored.sort(key=lambda x: -x["점수"])

    fields = ("방향수", "표시면수") if same_direction else ("표시면수",)
    if CHECK_SUPPORTS:
        fields += ("지지대수",)
    required = tuple(key for key in fields if our.get(key) is not None)
    required += tuple(key for key in ("설치방식", "발광방식", "반사방식") if our.get("속성", {}).get(key))
    if on_stage:
        on_stage('enriching')
    ranked, detail_count = [], 0
    for c in scored:
        if set(missing_product_fields(c, required)) - {"용도"} and not c["_raw"].get("_detail_checked_at"):
            if detail_count < max(top_n * 8, 24):
                detail_count += 1
                c = extract(provider.enrich(c["_raw"], required=required))
                if metadata is not None:
                    c["catalog_metadata"] = metadata
            else:
                counts["상세조회 한도 초과"] += 1
        reason = _price_or_identity_reason(our, c, price, lo, hi, exact, blocked) or _exclusion_reason(
            our, c, same_direction=same_direction)
        if reason:
            counts["탈락: " + reason] += 1
            continue
        if c['식별번호'] in excluded_ids or (excluded_ids and not c['식별번호']):
            counts["탈락: 제외 제품"] += 1
            continue
        if required_material:
            wanted = material_profile(required_material)
            actual = material_profile(c.get("재질원문") or c.get("재질", ""))
            if not wanted.families <= actual.families or not wanted.grades <= actual.grades:
                counts["탈락: 요청 재질"] += 1
                continue
        rank = material_rank(our_mat, c.get("재질원문") or c.get("재질", ""))
        if our_mat and (c.get("재질원문") or c.get("재질")) and not rank:
            counts["탈락: 재질"] += 1
            continue
        c["비교구분"], c["확인사항"] = _review_status(our, c)
        counts[c["비교구분"]] += 1
        c["점수"], c["점수상세"] = score(our, c)
        c["규격가격우선"] = _preferred(our, c)
        c["매칭근거"] = "동일 재질 명칭" if rank == 3 else "재질 계열 일치·세부 사양 확인" if rank == 2 else "혼합 재질 포함" if rank == 1 else "재질 미확인"
        c["매칭근거"] += f" / 지지대 {c['지지대수']}개 일치" if CHECK_SUPPORTS else " / 지지대 검사 생략"
        c["매칭근거"] += f" / 규격 각 변 ±{SPEC_TOL:.0%} 이내"
        c["매칭근거"] += " / 방향 수 일치 적용" if same_direction else " / 방향 수 차이 허용"
        if not c["규격가격우선"]:
            c["매칭근거"] += " / 작은 규격·높은 가격 조건 미충족"
        c["매칭근거"] = " / ".join([c["비교구분"], c["매칭근거"], *c["확인사항"]])
        ranked.append((rank, c))
    if on_stage:
        on_stage('selecting')
    counts["상세조회"] = detail_count
    ranked.sort(key=lambda rc: (REVIEW_ORDER[rc[1]["비교구분"]], -rc[0], -rc[1]["규격가격우선"], -rc[1]["점수"],
                                rc[1]["가격"] or Decimal(0), str(rc[1]["식별번호"])))

    top, seen = [], set()
    for _, c in ranked:
        company = norm_company(c.get("업체"))
        if not c.get("업체") or company in seen:
            continue
        seen.add(company)
        top.append(c)
        if len(top) >= top_n:
            break
    return sorted(top, key=lambda c: (c["가격"] or Decimal(0), str(c["식별번호"])))


def _price_or_identity_reason(our, candidate, price, lo, hi, exact, blocked):
    amount = candidate["가격"]
    if amount is None or amount <= 0 or (lo and amount < lo) or (hi and amount > hi) or (not exact and amount <= price):
        return "가격"
    if not candidate.get("업체") or norm_company(candidate["업체"]) in blocked or norm_company(candidate["업체"]) == norm_company(our.get("업체")):
        return "업체"
    return "동일 제품" if candidate["식별번호"] and candidate["식별번호"] == our.get("식별번호") else ""
