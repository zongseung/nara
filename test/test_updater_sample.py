import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
from dataclasses import replace
from openpyxl import load_workbook
from src.updater.exports import export_sample
from src.updater.store import StoreError
from test.updater_fixtures import FixtureSource, row


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'sample.xlsx'

    def test_sample_is_bounded_and_explicitly_incomplete(self):
        products = [dict(row(str(i)), itemIdnfNm='=1+1', itemIndfNmView='=1+1') for i in range(250)]
        source = FixtureSource(products)
        report = export_sample(source, self.out, limit=150)
        self.assertEqual(report['collected_items'], 150)
        self.assertEqual(report['source_total'], 250)
        self.assertFalse(report['coverage_complete'])
        self.assertEqual(len(source.calls), 2)
        self.assertFalse((self.root / 'control').exists())
        book = load_workbook(self.out, read_only=True, data_only=False)
        self.addCleanup(book.close)
        self.assertEqual(len(list(book['상품_테스트'].values)), 151)
        self.assertEqual(book['상품_테스트']['C2'].value, '=1+1')
        self.assertEqual(book['상품_테스트']['C2'].data_type, 's')

    def test_invalid_limits_and_existing_output_do_not_fetch(self):
        source = FixtureSource([row()])
        for limit in (0, -1, 1001):
            with self.assertRaises(StoreError):
                export_sample(source, self.out, limit=limit)
        self.out.write_bytes(b'existing')
        with self.assertRaises(StoreError):
            export_sample(source, self.out)
        self.assertEqual(self.out.read_bytes(), b'existing')
        self.assertEqual(source.calls, [])

    def test_cli_sample_does_not_initialize_operational_store(self):
        from src.updater.cli import main
        data = self.root / 'unused-operational-data'
        source = FixtureSource([row()])
        output = io.StringIO()
        with patch('sys.argv', ['cli', '--data-dir', str(data), 'sample', '--out', str(self.out)]), \
                patch('src.updater.source.ShoppingSource', return_value=source), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())['collected_items'], 1)
        self.assertFalse(data.exists())

    def test_repeated_or_short_page_leaves_no_output(self):
        class BadSource(FixtureSource):
            def fetch(self, query):
                page = super().fetch(replace(query, page=1))
                return replace(page, rows=()) if self.short and query.page == 2 else page
        for short in (False, True):
            source = BadSource([row(str(i)) for i in range(250)])
            source.short = short
            with self.assertRaises(StoreError):
                export_sample(source, self.out, limit=150)
            self.assertFalse(self.out.exists())
