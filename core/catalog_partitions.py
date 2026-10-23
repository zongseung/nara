"""Exact inclusive Decimal partitions shared by operational and diagnostic collection."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable
from core.catalog_sources import SourceError


@dataclass(frozen=True, slots=True)
class PriceDomain:
    lower: Decimal
    upper: Decimal
    quantum: Decimal
    max_partition_rows: int = 1000

    def __post_init__(self) -> None:
        if (not all(x.is_finite() for x in (self.lower, self.upper, self.quantum))
                or self.lower < 0 or self.upper < self.lower or self.quantum <= 0
                or (self.upper - self.lower) % self.quantum or self.max_partition_rows < 1):
            raise SourceError('invalid_price_domain')


@dataclass(frozen=True, slots=True)
class Partition:
    lower: Decimal | None
    upper: Decimal | None
    count: int

    def contains(self, price: Decimal, quantum: Decimal | None) -> bool:
        return self.lower is None or (self.upper is not None and self.lower <= price <= self.upper
                and (quantum is None or (price - self.lower) % quantum == 0))


def plan_partitions(domain: PriceDomain, count: Callable[[Decimal, Decimal], int]) -> tuple[Partition, ...]:
    pending = [(domain.lower, domain.upper)]
    result = []
    while pending:
        lower, upper = pending.pop()
        total = count(lower, upper)
        if total <= domain.max_partition_rows:
            result.append(Partition(lower, upper, total))
            continue
        steps = (upper - lower) // domain.quantum
        if steps == 0:
            raise SourceError('coverage_unresolved', 'single_price_overflow')
        middle = lower + (steps // 2) * domain.quantum
        pending.extend(((middle + domain.quantum, upper), (lower, middle)))
    return tuple(result)
