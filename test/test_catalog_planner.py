import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from src.catalog_client import FixtureSource
from core.catalog_partitions import PriceDomain
from src.catalog_planner import make_plan
from core.catalog_sources import SourceError, get_profile

FIXTURE = Path("test/fixtures/diagnostic/rows.json")


class PlannerTests(unittest.TestCase):
    def test_decimal_split_reuses_shared_partition_algorithm(self):
        # Given a known Decimal domain; when planned; then inclusive adjacent coverage is exact.
        source = FixtureSource(FIXTURE, get_profile("shopping-all"))
        plan = make_plan(
            source, PriceDomain(Decimal(110), Decimal(111), Decimal(".5"), 103)
        )
        self.assertEqual(sum(p.count for p in plan.partitions), 205)
        self.assertEqual(plan.unresolved, ())
        self.assertEqual(
            plan.partitions[0].upper + Decimal(".5"), plan.partitions[1].lower
        )

    def test_missing_domain_rows_are_unresolved(self):
        # Given a domain excluding half the fixture; when planned; then coverage blocks publication.
        source = FixtureSource(FIXTURE, get_profile("shopping-all"))
        plan = make_plan(source, PriceDomain(Decimal(110), Decimal(110), Decimal(1)))
        self.assertIn("price_domain_coverage", plan.unresolved)

    def test_single_price_overflow_is_not_deep_paged(self):
        # Given an indivisible oversized partition; when planned; then a typed unresolved error.
        source = FixtureSource(FIXTURE, get_profile("shopping-all"))
        with self.assertRaises(SourceError) as caught:
            make_plan(source, PriceDomain(Decimal(110), Decimal(110), Decimal(1), 10))
        self.assertEqual(caught.exception.code, "coverage_unresolved")

    def test_count_drift_during_plan_is_retained(self):
        # Given a source replacing its end count; when planned; then the plan remains unresolved.
        source = FixtureSource(FIXTURE, get_profile("shopping-all"))
        original = source.fetch
        calls = []

        def fetch(query):
            calls.append(query)
            page = original(query)
            return replace(page, row_total=206) if len(calls) == 3 else page

        source.fetch = fetch
        plan = make_plan(source, PriceDomain(Decimal(110), Decimal(111), Decimal(1)))
        self.assertIn("planning_count_drift", plan.unresolved)
