"""
ponytail 자체검증: 공용 plan_partitions()가 가격범위를 빈틈/중복 없이 덮고,
각 리프가 max_partition_rows 이하(또는 단일가격)인지 확인. 네트워크 불필요(count를 스텁으로 대체).
  실행:  python -m unittest test.test_crawl
"""
import unittest
from decimal import Decimal
from itertools import pairwise

from core.catalog_partitions import PriceDomain, plan_partitions
from core.catalog_sources import SourceError


def _run(distribution, lo, hi, safe):
    """distribution(price)->count 로 count를 스텁하고 plan_partitions 실행."""
    def fake_count(a, b):
        return sum(distribution(p) for p in range(int(a), int(b) + 1))
    parts = plan_partitions(PriceDomain(Decimal(lo), Decimal(hi), Decimal(1), safe), fake_count)
    return sorted((int(p.lower), int(p.upper), p.count) for p in parts)


class CrawlTests(unittest.TestCase):
    def test_covers_contiguously_and_bounded(self):
        # 가격단위당 1건 균일분포
        leaves = _run(lambda p: 1, 0, 999, safe=100)
        assert leaves[0][0] == 0 and leaves[-1][1] == 999          # 양끝 커버
        for (a, b, tot), (na, nb, ntot) in pairwise(leaves):
            assert b + 1 == na, (b, na)                            # 빈틈/중복 없음
        for lo, hi, tot in leaves:
            assert tot <= 100 or lo == hi, (lo, hi, tot)           # 각 리프 상한 이하


    def test_total_count_conserved_and_single_price_overflow_rejected(self):
        leaves = _run(lambda p: 50 if p == 500 else 1, 0, 999, safe=100)
        assert sum(t for _, _, t in leaves) == 999 + 50
        with self.assertRaises(SourceError) as failure:              # 한 가격에 상한 초과 → 더 못 쪼갬
            _run(lambda p: 5000 if p == 500 else 1, 0, 999, safe=100)
        assert failure.exception.field == "single_price_overflow"


    def test_empty_range_is_one_partition(self):
        leaves = _run(lambda p: 0, 0, 999, safe=100)
        assert leaves == [(0, 999, 0)]                             # 0건이면 분할 없이 한 구간


if __name__ == "__main__":
    unittest.main()
