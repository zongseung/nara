import json
from decimal import Decimal, localcontext
from pathlib import Path
import tempfile
import unittest
from src.matching.index import decimal_key
from src.updater.collector import refresh, CollectionPolicy
from src.updater.reader import open_published_snapshot
from src.updater.store import Store
from test.updater_fixtures import FixtureSource, row


class DecimalFidelityTests(unittest.TestCase):
    def test_distinct_numeric_prices_survive_stage_history_and_index(self):
        with tempfile.TemporaryDirectory() as directory, localcontext() as context:
            context.prec = 6
            store = Store(Path(directory))
            values = ['100.00000000000000000000000001', '100.00000000000000000000000002']
            for tick, value in enumerate(values):
                raw = json.loads(json.dumps(row()).replace('"100"', value), parse_float=Decimal)
                manifest = refresh(store, FixtureSource([raw], tick), policy=CollectionPolicy(request_interval=0), environment='fixture')
            with open_published_snapshot(manifest, root=store.root) as reader:
                current = list(reader.current())[0]
                self.assertEqual(current['contract_price_decimal'], values[-1])
                prices = list(reader.history(current['listing_key'], price_only=True))
                self.assertEqual([p['new_amount'] for p in prices], values)
                evidence = reader.connection.execute('SELECT raw_json FROM observation_evidence ORDER BY observed_at').fetchall()
                self.assertEqual([json.loads(r[0], parse_float=Decimal)['ctrtUprc'] for r in evidence], list(map(Decimal, values)))
            self.assertNotEqual(decimal_key(Decimal(values[0])), decimal_key(Decimal(values[1])))
            self.assertEqual(decimal_key(Decimal('100.00')), '100')

    def test_shared_json_keeps_types_and_rejects_nonfinite(self):
        from core.catalog_json import json_text
        from src.catalog_store import json_text as diagnostic_json
        self.assertIs(diagnostic_json, json_text)
        original = {'numeric': Decimal('1.00000000000000000000000001'), 'text': '1.00'}
        self.assertEqual(json.loads(json_text(original), parse_float=Decimal), original)
        for value in (Decimal('NaN'), Decimal('Infinity'), float('inf')):
            with self.assertRaises(ValueError):
                json_text(value)
        self.assertEqual(decimal_key(Decimal('-0.00')), '0')

    def test_http_numeric_token_and_original_response_survive_checkpoint(self):
        from dataclasses import replace
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from core.catalog_sources import get_profile
        from src.transport import RequestLimiter
        from src.g2b_client import fetch_search_page
        from src.updater.collector import collect
        from src.updater.publisher import publish
        price = '100.00000000000000000000000002'
        product = row(price=price)
        product['totCnt'] = 1
        wire = json.dumps({'totalSize': 1, 'rsltList': [product]}, indent=2).replace('"' + price + '"', price)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(wire.encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        endpoint = replace(get_profile('shopping-all'), endpoint=f'http://127.0.0.1:{server.server_port}/')

        class Source:
            limiter = RequestLimiter(rate=10000)

            def fetch(self, query):
                page = fetch_search_page(query, endpoint, limiter=self.limiter)
                return replace(page, profile=get_profile('shopping-all'))

        try:
            with tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory))
                with store.writer(get_profile('shopping-all'), environment='fixture') as lease:
                    collect(store, lease, Source(), policy=CollectionPolicy(request_interval=0))
                    with store.stage(lease) as stage:
                        self.assertEqual(stage.execute('SELECT raw_json FROM pages').fetchone()[0], wire)
                        self.assertEqual(json.loads(stage.execute('SELECT raw_json FROM observations').fetchone()[0], parse_float=Decimal)['ctrtUprc'], Decimal(price))
                    manifest = publish(store, lease)
                with open_published_snapshot(manifest, root=store.root) as reader:
                    self.assertEqual(list(reader.current())[0]['contract_price_decimal'], price)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_equivalent_numeric_and_string_price_keep_raw_types_without_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            for tick, price in enumerate((Decimal('100.00'), '100.00')):
                manifest = refresh(store, FixtureSource([row(price=price)], tick), policy=CollectionPolicy(request_interval=0), environment='fixture')
            with open_published_snapshot(manifest, root=store.root) as reader:
                raw = [json.loads(r[0], parse_float=Decimal)['ctrtUprc'] for r in reader.connection.execute('SELECT raw_json FROM observation_evidence ORDER BY observed_at')]
                self.assertEqual(raw, [Decimal('100.00'), '100.00'])
                self.assertEqual(reader.connection.execute('SELECT count(*) FROM price_history').fetchone()[0], 1)
