"""기준 품목 하나의 경쟁사 비교: 필수 구성 검사 → 부족한 속성만 상세 보강 → 재질·규격·가격 순 선정.

조회는 주입된 CandidateProvider만 거치므로 실시간·진단 DB·테스트 대역이 같은 규칙으로 판정된다."""

from collections.abc import Iterable
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
    normalize_use,
    score,
)
from core.matching.normalization import material_rank

MAX_POOL: Final = 500


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


def compatible(our, candidate, allow_missing=False):
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
    return not (left and normalize_use(left) != normalize_use(right) and (right or not allow_missing))


def _preferred(our, candidate):
    left, right = our.get("규격"), candidate.get("규격")
    return bool(left and right and candidate["가격"] > (our.get("가격") or 0)
                and all(c <= o for o, c in zip(left, right)) and any(c < o for o, c in zip(left, right)))


def compare(our: ComparisonItem, provider: CandidateProvider, *, top_n: int = 3, exact_price: bool = False,
            fallback_band: float = 0.0, excluded: Iterable[str] = ()) -> tuple[ComparisonItem, list[ComparisonItem]]:
    """(보강·보류 사유를 반영한 기준 품목, 가격 오름차순 경쟁사 최대 top_n).
    기준의 규격·지지대 개수가 미확인이면 비교를 보류한다. 기본은 기준보다 높은 가격의 후보.
    exact_price=True: 같은 단가만. 하나도 없고 fallback_band>0이면 기준 초과~+band 가격대로 재검색(근사).
    excluded: 제외할 업체명(정규화 exact 비교). 자기·계열사 매칭 방지."""
    raw = our.get("_raw")
    if raw:
        enriched = provider.enrich(raw, required=("지지대수",))
        if enriched is not raw:
            our = {**our, **extract(enriched)}
    missing = [key for key in ("규격", "지지대수") if our.get(key) is None]
    if missing:
        return {**our, "비교보류사유": "비교 보류: " + ", ".join(missing) + " 미확인"}, []
    blocked = {norm_company(name) for name in excluded}
    price = Decimal(str(our.get("가격") or 0))
    if not (exact_price and price > 0):
        return our, _select(our, provider, price, price or "", "", top_n, blocked)
    found = _select(our, provider, price, price, price, top_n, blocked, exact=True)
    if found or fallback_band <= 0:
        return our, found
    band = Decimal(str(fallback_band))
    approx = _select(our, provider, price, price * (1 - band), price * (1 + band), top_n, blocked)
    return our, [{**c, "근사": True} for c in approx]


def _select(our, provider, price, lo, hi, top_n, blocked, *, exact=False):
    keyword = our.get("품명", "") + (" " + our["용도"] if our.get("용도") else "")
    our_mat = our.get("재질원문") or our.get("재질", "")
    result = provider.search(keyword, str(lo), str(hi), MAX_POOL)
    metadata = dict(asdict(result.metadata), total=result.total, truncated=result.truncated) if result.metadata else None

    scored = []
    for row in result.rows:
        c = extract(row)
        amount = c["가격"]
        if amount is None or amount <= 0 or (c["식별번호"] and c["식별번호"] == our.get("식별번호")):
            continue
        if norm_company(c.get("업체")) in blocked or (lo and amount < lo) or (hi and amount > hi):
            continue
        if (not exact and amount <= price) or not compatible(our, c, allow_missing=True):
            continue
        if metadata is not None:
            c["catalog_metadata"] = metadata
        c["점수"], c["점수상세"] = score(our, c)
        scored.append(c)
    scored.sort(key=lambda x: -x["점수"])

    required = tuple(key for key in ("방향수", "표시면수", "지지대수") if our.get(key) is not None)
    required += tuple(key for key in ("설치방식", "발광방식", "반사방식") if our.get("속성", {}).get(key))
    ranked, detail_count = [], 0
    for c in scored:
        if missing_product_fields(c, required) and not c["_raw"].get("_detail_checked_at"):
            if detail_count >= max(top_n * 8, 24):
                continue
            detail_count += 1
            c = extract(provider.enrich(c["_raw"], required=required))
            if metadata is not None:
                c["catalog_metadata"] = metadata
        if not compatible(our, c):
            continue
        rank = material_rank(our_mat, c.get("재질원문") or c.get("재질", ""))
        if our_mat and not rank:
            continue
        c["점수"], c["점수상세"] = score(our, c)
        c["규격가격우선"] = _preferred(our, c)
        c["매칭근거"] = "동일 재질 명칭" if rank == 3 else "재질 계열 일치·세부 사양 확인" if rank == 2 else "혼합 재질 포함" if rank == 1 else "재질 미확인"
        c["매칭근거"] += f" / 지지대 {c['지지대수']}개 일치 / 규격 각 변 ±{SPEC_TOL:.0%} 이내"
        if not c["규격가격우선"]:
            c["매칭근거"] += " / 작은 규격·높은 가격 조건 미충족"
        ranked.append((rank, c))
    ranked.sort(key=lambda rc: (-rc[0], -rc[1]["규격가격우선"], -rc[1]["점수"],
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
