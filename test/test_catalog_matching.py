import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from src import build_report
from src import matcher
from core.catalog_partitions import Partition
from src.catalog_planner import CollectionPlan
from src.catalog_provider import SQLiteProvider
from core.catalog_sources import SourceError, get_profile
from src.catalog_store import CatalogStore
from src.g2b_client import parse_search_page


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.db = self.path / "db.sqlite"
        profile = get_profile("shopping-all")
        with CatalogStore(self.db) as store:
            self.run = store.create_run(
                profile, CollectionPlan((Partition(None, None, 1),), 1, 1)
            )
            store.save_page(
                self.run,
                0,
                1,
                parse_search_page(
                    {
                        "totalSize": 1,
                        "rsltList": [
                            {
                                "ctrtItemMngNo": "a",
                                "itemIndfNmView": "ＡＢ 안내판 목재",
                                "ctrtUprc": "110",
                                "totCnt": 1,
                                "mnftrEtpsNm": "경쟁사",
                                "imgSrc": "https://invalid/image.png",
                            }
                        ],
                    },
                    profile,
                ),
            )
        self.our = {"품명": "안내판", "재질": "목재", "가격": 100}

    def test_partial_requires_explicit_allow_and_nfkc_tokens_are_and(self):
        # Given an incomplete diagnostic run.
        with self.assertRaises(SourceError):
            SQLiteProvider(self.db, self.run)
        provider = SQLiteProvider(self.db, self.run, allow_incomplete=True)
        # When searching normalized tokens.
        result = provider.search("ab 안내판")
        # Then both tokens match and the incomplete provenance remains attached.
        self.assertEqual(result.total, 1)
        self.assertTrue(result.metadata.incomplete)
        self.assertEqual(provider.search("ab missing").total, 0)

    def test_offline_approximate_retry_keeps_run_and_report_has_urls(self):
        # Given a local provider and all HTTP forbidden.
        provider = SQLiteProvider(self.db, self.run, allow_incomplete=True)
        with patch(
            "requests.Session.request", side_effect=AssertionError("network forbidden")
        ):
            # When exact price misses and approximate matching renders a workbook.
            candidates = matcher.find_candidates(
                self.our, exact_price=True, fallback_band=0.3, provider=provider
            )
            out = self.path / "report.xlsx"
            build_report.build_multi(
                {"우리": [self.our]},
                out,
                exact_price=True,
                fallback_band=0.3,
                provider=provider,
            )
        # Then the same run supplies candidates and image URLs remain text.
        self.assertTrue(candidates[0]["근사"])
        self.assertEqual(candidates[0]["catalog_metadata"]["run_id"], self.run)
        with out.open("rb") as stream:
            wb = load_workbook(stream)
            values = [c.value for row in wb.active for c in row]
            self.assertIn("https://invalid/image.png", values)
            self.assertTrue(any(self.run in str(v) for v in values))
            wb.close()
