"""Immutable diagnostic partition plans, using the shared exact price planner."""

from dataclasses import dataclass
from decimal import Decimal

from src.catalog_client import Source
from core.catalog_partitions import Partition, PriceDomain, plan_partitions
from core.catalog_sources import SearchQuery, SourceError


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    partitions: tuple[Partition, ...]
    expected: int
    total_size: int
    quantum: Decimal | None = None
    unresolved: tuple[str, ...] = ()
    version: str = "diagnostic-plan/1"


def make_plan(source: Source, domain: PriceDomain | None = None) -> CollectionPlan:
    first = source.fetch(SearchQuery(page_size=1))
    total = first.row_total if first.row_total is not None else first.total_size
    if domain is None:
        if total > 1000:
            raise SourceError(
                "coverage_unresolved", "explicit_decimal_price_domain_required"
            )
        return CollectionPlan((Partition(None, None, total),), total, first.total_size)

    def count(lo: Decimal, hi: Decimal) -> int:
        page = source.fetch(
            SearchQuery(page_size=1, start_price=str(lo), end_price=str(hi))
        )
        return page.row_total if page.row_total is not None else page.total_size

    parts = plan_partitions(domain, count)
    end = source.fetch(SearchQuery(page_size=1))
    unresolved = []
    if sum(p.count for p in parts) != total:
        unresolved.append("price_domain_coverage")
    if (
        end.row_total if end.row_total is not None else end.total_size
    ) != total or end.total_size != first.total_size:
        unresolved.append("planning_count_drift")
    return CollectionPlan(
        parts, total, first.total_size, domain.quantum, tuple(unresolved)
    )
