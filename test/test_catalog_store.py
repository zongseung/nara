import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.catalog_partitions import Partition
from src.catalog_planner import CollectionPlan
from core.catalog_sources import SourceError, get_profile
from src.catalog_store import CatalogStore
from src.g2b_client import parse_search_page


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "catalog_v2.db"
        self.profile = get_profile("shopping-all")

    def test_raw_identity_and_conflicts_survive(self):
        # Given two contracts for one goods number and an unidentified observation.
        rows = [
            {
                "ctrtItemMngNo": k,
                "itemIdnfNo": "goods",
                "ctrtUprc": "10.25",
                "totCnt": 3,
                "unknown": {"nested": [True, None]},
                "parent": "p",
            }
            for k in ("a", "b")
        ]
        rows.append({"totCnt": 3, "ctrtUprc": None})
        with CatalogStore(self.db) as store:
            run = store.create_run(
                self.profile, CollectionPlan((Partition(None, None, 3),), 3, 3)
            )
            # When committing one whole page.
            store.save_page(
                run,
                0,
                1,
                parse_search_page({"totalSize": 3, "rsltList": rows}, self.profile),
            )
            # Then raw rows and checkpoint coexist; missing identity is quarantined.
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 3
            )
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM run_entities").fetchone()[0], 2
            )
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM quarantine").fetchone()[0], 1
            )
            self.assertEqual(
                json.loads(
                    store.db.execute(
                        "SELECT raw_json FROM observations LIMIT 1"
                    ).fetchone()[0]
                ),
                rows[0],
            )

    def test_legacy_database_is_untouched(self):
        # Given a legacy database.
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE items(id TEXT, data TEXT)")
        before = self.db.read_bytes()
        # When opening as diagnostic storage.
        with self.assertRaises(SourceError), CatalogStore(self.db):
            self.fail("legacy opened")
        # Then its bytes are unchanged.
        self.assertEqual(self.db.read_bytes(), before)

    def test_process_death_before_and_after_commit_is_atomic(self):
        # Given a real database and subprocesses terminated inside/after save_page.
        import subprocess
        import sys

        with CatalogStore(self.db) as store:
            run = store.create_run(
                self.profile, CollectionPlan((Partition(None, None, 1),), 1, 1)
            )
        template = """
import os
from src.catalog_store import CatalogStore
from src.g2b_client import parse_search_page
from core.catalog_sources import SourceError, get_profile
with CatalogStore({db!r}) as store:
    {trigger}
    store.save_page({run!r}, 0, 1, parse_search_page({{'totalSize':1,'rsltList':[{{'totCnt':1,'ctrtItemMngNo':'a','ctrtUprc':'1'}}]}}, get_profile('shopping-all')))
    os._exit(24)
"""
        trigger = "store.db.create_function('die', 0, lambda: os._exit(23)); store.db.execute('CREATE TEMP TRIGGER crash BEFORE INSERT ON observations BEGIN SELECT die(); END')"
        # When killed after the page insert but before observation/commit.
        first = subprocess.run(
            [
                sys.executable,
                "-c",
                template.format(db=str(self.db), run=run, trigger=trigger),
            ],
            check=False,
        )
        with CatalogStore(self.db) as store:
            self.assertEqual(first.returncode, 23)
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 0
            )
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0
            )
        # Then a kill after successful commit retains both checkpoint and row.
        second = subprocess.run(
            [
                sys.executable,
                "-c",
                template.format(db=str(self.db), run=run, trigger="pass"),
            ],
            check=False,
        )
        with CatalogStore(self.db) as store:
            self.assertEqual(second.returncode, 24)
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 1
            )
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 1
            )

    def test_decimal_numeric_raw_roundtrip_and_alias_conflict(self):
        # Given an exact numeric price and conflicting raw name aliases.
        from decimal import Decimal

        raw = {
            "ctrtItemMngNo": "a",
            "totCnt": 1,
            "ctrtUprc": Decimal("10.00000000000000000001"),
            "itemIdnfNm": "first",
            "itemIndfNmView": "second",
        }
        with CatalogStore(self.db) as store:
            run = store.create_run(
                self.profile, CollectionPlan((Partition(None, None, 1),), 1, 1)
            )
            # When persisting the page; then exact JSON numeric value and both aliases survive.
            store.save_page(
                run,
                0,
                1,
                parse_search_page({"totalSize": 1, "rsltList": [raw]}, self.profile),
            )
            encoded = store.db.execute("SELECT raw_json FROM observations").fetchone()[
                0
            ]
            self.assertEqual(json.loads(encoded, parse_float=Decimal), raw)
            self.assertEqual(
                store.db.execute("SELECT reason FROM quarantine").fetchone()[0],
                "conflicting_name_aliases",
            )
