import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
from openpyxl import load_workbook

from src.catalog_client import FixtureSource
from src.catalog_planner import make_plan
from core.catalog_sources import SourceError, get_profile
from src.catalog_store import CatalogStore
from src.catalog_verify import reconcile
from src.crawl_all import collect
from src.export_parquet import export_catalog
from test.test_catalog_planner import FIXTURE


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.db = self.path / "db.sqlite"
        source = FixtureSource(FIXTURE, get_profile("shopping-all"))
        with CatalogStore(self.db) as store:
            self.run = store.create_run(source.profile, make_plan(source))
            collect(store, source, self.run)
            reconcile(store, self.run)

    def test_parquet_roundtrip_preserves_keys_raw_and_metadata(self):
        # Given reconciled SQLite; when exported with two small streaming batches.
        out = self.path / "out.parquet"
        with patch("src.export_parquet.BATCH", 100):
            count = export_catalog(self.db, out)
        # Then all entity keys, unknown raw fields and provenance survive.
        table = pq.read_table(out)
        with sqlite3.connect(self.db) as con:
            keys = {r[0] for r in con.execute("SELECT entity_key FROM run_entities")}
        self.assertEqual(set(table.column("entity_key").to_pylist()), keys)
        self.assertEqual((count, table.num_rows), (205, 205))
        self.assertEqual(
            json.loads(table.column("raw_json")[0].as_py())["extra"]["unicode"], "한글"
        )
        self.assertEqual(
            json.loads(table.schema.metadata[b"catalog"])["validation"],
            "reconciled_observation",
        )

    def test_xlsx_splits_with_header_in_row_limit(self):
        # Given a small test row limit; when exported; then each sheet includes its header in the cap.
        out = self.path / "out.xlsx"
        export_catalog(self.db, out, xlsx_rows=101)
        wb = load_workbook(out, read_only=True)
        try:
            sheets = wb.worksheets[1:]
            self.assertEqual([sum(1 for _ in s.rows) for s in sheets], [101, 101, 6])
        finally:
            wb.close()

    def test_failed_output_does_not_replace_existing_file(self):
        # Given a previous export; when the atomic replacement fails; then old bytes remain.
        out = self.path / "out.parquet"
        out.write_bytes(b"previous")
        with (
            patch("src.export_parquet.os.replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            export_catalog(self.db, out)
        self.assertEqual(out.read_bytes(), b"previous")
        self.assertEqual(list(self.path.glob("*.partial")), [])

    def test_incomplete_export_is_refused_without_override(self):
        # Given a run whose validation is incomplete; when exported; then explicit opt-in is required.
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET validation='incomplete'")
        with self.assertRaises(SourceError):
            export_catalog(self.db, self.path / "out.parquet", run=self.run)
        export_catalog(
            self.db, self.path / "out.parquet", run=self.run, allow_incomplete=True
        )
        self.assertTrue(
            json.loads(pq.read_schema(self.path / "out.parquet").metadata[b"catalog"])[
                "incomplete"
            ]
        )

    def test_legacy_items_export_marks_data_unverified(self):
        # Given an old normalized-items database; when exported; then no raw-original claim is made.
        legacy = self.path / "legacy.sqlite"
        with sqlite3.connect(legacy) as con:
            con.execute("CREATE TABLE items(id TEXT, data TEXT)")
            con.execute("INSERT INTO items VALUES(?,?)", ("a", '{"품명":"안내판"}'))
        out = self.path / "legacy.parquet"
        export_catalog(legacy, out)
        metadata = json.loads(pq.read_schema(out).metadata[b"catalog"])
        self.assertEqual(metadata["validation"], "legacy_unverified")
        self.assertFalse(metadata["raw_original_available"])

    def test_writer_failure_preserves_existing_output(self):
        # Given a previous file; when the writer cannot open disk storage; then no replacement occurs.
        out = self.path / "out.parquet"
        out.write_bytes(b"previous")
        with (
            patch("src.export_parquet.pq.ParquetWriter", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            export_catalog(self.db, out)
        self.assertEqual(out.read_bytes(), b"previous")
        self.assertFalse(list(self.path.glob("*.partial")))

    def test_formula_like_legacy_identifier_is_text(self):
        # Given an attacker-controlled legacy ID; when exported to XLSX; then it cannot become a formula.
        legacy = self.path / "legacy.sqlite"
        with sqlite3.connect(legacy) as con:
            con.execute("CREATE TABLE items(id TEXT, data TEXT)")
            con.execute("INSERT INTO items VALUES(?,?)", ('=HYPERLINK("bad")', "{}"))
        out = self.path / "legacy.xlsx"
        export_catalog(legacy, out)
        book = load_workbook(out)
        try:
            cell = book["data_1"]["D2"]
            self.assertEqual(cell.data_type, "s")
            self.assertTrue(cell.value.startswith("'="))
        finally:
            book.close()
