import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.catalog_sources import SearchQuery, SourceError, get_profile
from src.g2b_client import enrich_product, fetch_page, fetch_search_page, parse_search_page
from core.legacy_matching import extract


class ClientTests(unittest.TestCase):
    def test_captured_totals_remain_distinct(self):
        capture = json.loads(Path('test/fixtures/source_contracts/ui_counts.json').read_text())['capture']
        body = {'ErrorCode': capture['errorCode'], 'totalSize': capture['totalSize'],
                'rsltList': [{'totCnt': capture['totCnt'], 'ctrtUprc': None, 'extra': 'kept'}]}
        page = parse_search_page(body, get_profile())
        self.assertEqual((page.total_size, page.row_total), (10076, 7861))
        self.assertEqual(page.rows[0].raw['extra'], 'kept')
        self.assertIsNone(page.rows[0].contract_price_decimal)

    def test_invalid_responses_never_become_empty(self):
        bodies = [{}, {'ErrorCode': 0, 'totalSize': 0},
                  {'ErrorCode': 1, 'rsltList': [], 'totalSize': 0},
                  {'rsltList': None, 'totalSize': 0}, {'rsltList': [], 'totalSize': -1},
                  {'rsltList': [], 'totalSize': True}, {'rsltList': [], 'totalSize': '1.2'},
                  {'rsltList': [], 'totalSize': 1}, {'rsltList': [1], 'totalSize': 1},
                  {'rsltList': [{}], 'totalSize': 1}, {'rsltList': [{'totCnt': True}], 'totalSize': 1}]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(SourceError):
                parse_search_page(body, get_profile())

    def test_only_explicit_zero_list_is_empty(self):
        page = parse_search_page({'ErrorCode': 0, 'totalSize': 0, 'rsltList': []}, get_profile())
        self.assertEqual((page.rows, page.total_size, page.row_total), ((), 0, None))

    def test_oversized_count_strings_raise_typed_errors(self):
        for field, body in (
            ('totalSize', {'totalSize': '9' * 5000, 'rsltList': []}),
            ('totCnt', {'totalSize': 1, 'rsltList': [{'totCnt': '9' * 5000}]}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(SourceError) as failure:
                    parse_search_page(body, get_profile())
                self.assertEqual((failure.exception.code, failure.exception.field), ('invalid_count', field))

    def test_invalid_prices_fail_common_boundary(self):
        for value in (True, [], '', 'NaN', 'Infinity', '-1'):
            with self.subTest(value=value), self.assertRaises(SourceError):
                parse_search_page({'totalSize': 1, 'rsltList': [{'totCnt': 1, 'ctrtUprc': value}]}, get_profile())


class WireTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.release = threading.Event()
        self.responses = [(200, b'{"ErrorCode":0,"totalSize":0,"rsltList":[]}')]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                status, body = owner.responses[min(len(owner.requests) - 1, len(owner.responses) - 1)]
                if status == 0:
                    owner.release.wait(2)
                    return
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.profile = replace(get_profile('shopping-all'), endpoint=f'http://127.0.0.1:{self.server.server_port}/')

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_retry_is_bounded_at_three_requests(self):
        self.responses = [(503, b'busy')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual((failure.exception.code, failure.exception.attempts, len(self.requests)), ('http_status', 3, 3))

    def test_transient_failure_can_recover_without_nested_retry(self):
        self.responses.insert(0, (500, b'busy'))
        page = fetch_search_page(SearchQuery(page_size=500), self.profile)
        self.assertEqual((page.attempts, len(self.requests)), (2, 2))
        self.assertEqual(self.requests[0]['searchVO']['recordCountPerPage'], '100')
        self.assertEqual(self.requests[0]['searchVO']['prchsMthdSeCd'], '')

    def test_timeout_is_typed_and_bounded(self):
        self.responses = [(0, b'')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile, timeout=.03)
        self.assertEqual((failure.exception.code, failure.exception.attempts, len(self.requests)), ('timeout', 3, 3))

    def test_html_200_is_failure_not_empty(self):
        self.responses = [(200, b'<html>error</html>')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual((failure.exception.code, len(self.requests)), ('invalid_json', 1))

    def test_permanent_http_failure_is_not_retried(self):
        self.responses = [(403, b'forbidden')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual((failure.exception.status, len(self.requests)), (403, 1))

    def test_wire_decimal_retains_exact_digits(self):
        self.responses = [(200, b'{"totalSize":1,"rsltList":[{"totCnt":1,"ctrtUprc":100.000000000000001}]}')]
        page = fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual(page.rows[0].contract_price_decimal, Decimal('100.000000000000001'))

    def test_single_outer_replay_is_at_most_six_requests(self):
        self.responses = [(503, b'busy')]
        failures = []
        for _ in range(2):
            try:
                fetch_search_page(SearchQuery(), self.profile)
            except SourceError as failure:
                failures.append(failure)
        self.assertEqual((len(failures), len(self.requests)), (2, 6))

    def test_nested_adapter_retries_rejected_before_request(self):
        with requests.Session() as session:
            session.mount('http://', requests.adapters.HTTPAdapter(max_retries=2))
            with self.assertRaises(SourceError) as failure:
                fetch_search_page(SearchQuery(), self.profile, session=session)
        self.assertEqual((failure.exception.code, len(self.requests)), ('nested_retries', 0))

    def test_legacy_tuple_preserves_raw_rows_and_row_count(self):
        self.responses = [(200, b'{"totalSize":2,"rsltList":[{"totCnt":1,"ctrtUprc":null,"extra":"kept"}]}')]
        with patch('src.g2b_client.get_profile', return_value=self.profile):
            rows, count = fetch_page(1)
        self.assertEqual((rows[0]['ctrtUprc'], rows[0]['extra'], count), (None, 'kept', 1))

    def test_business_error_is_not_retried_or_treated_as_empty(self):
        self.responses = [(200, b'{"ErrorCode":99,"totalSize":0,"rsltList":[]}')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual((failure.exception.code, failure.exception.attempts, len(self.requests)), ('business_error', 1, 1))

    def test_wire_oversized_count_string_is_typed_with_http_metadata(self):
        body = json.dumps({'totalSize': '9' * 5000, 'rsltList': []}).encode()
        self.responses = [(200, body)]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual((failure.exception.code, failure.exception.field,
                          failure.exception.attempts, failure.exception.status, len(self.requests)),
                         ('invalid_count', 'totalSize', 1, 200, 1))

    def test_wire_oversized_json_integer_is_typed(self):
        self.responses = [(200, b'{"totalSize":' + b'9' * 5000 + b',"rsltList":[]}')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile)
        self.assertEqual((failure.exception.code, failure.exception.attempts,
                          failure.exception.status, len(self.requests)), ('invalid_json', 1, 200, 1))

    def test_unknown_adapter_is_typed_before_request(self):
        class CustomAdapter(requests.adapters.BaseAdapter):
            def send(self, *args, **kwargs):
                raise AssertionError('unsupported transport must be rejected before send')

            def close(self):
                return

        with requests.Session() as session:
            session.mount('http://', CustomAdapter())
            with self.assertRaises(SourceError) as failure:
                fetch_search_page(SearchQuery(), self.profile, session=session)
        self.assertEqual((failure.exception.code, len(self.requests)), ('unsupported_adapter', 0))

    def test_detail_endpoint_supplies_missing_attributes_and_does_not_replace_search_fields(self):
        self.responses = [(200, json.dumps({'ErrorCode':0,'dlPdctAtrbInfoL':[
            {'atrbnm':'재질','atrbvalue':'알루미늄 A5052-H32/PC','atrboptnvalue':None},
            {'atrbnm':'용도','atrbvalue':'2방향안내','atrboptnvalue':None},
            {'atrbnm':'표지크기(가로)','atrbvalue':'600','atrboptnvalue':'mm'},
            {'atrbnm':'표지크기(세로)','atrbvalue':'150','atrboptnvalue':'mm'}]}).encode())]
        raw = {'itemIdnfNo':'12345678','itemClsfNo':'55121718','ctrtUprc':'125.5'}
        with patch('src.g2b_client.get_profile', return_value=self.profile):
            enriched = enrich_product(raw)
            item = extract(enriched)
            again = enrich_product(enriched)
        self.assertEqual(item['재질원문'], '알루미늄 A5052-H32/PC')
        self.assertEqual(item['규격'], (600,150))
        self.assertEqual(item['방향수'],2)
        self.assertEqual(again, enriched)
        self.assertNotIn('_detail_attributes',raw)
        self.assertEqual(self.requests,[{'dlGdsDtlInfoSrchM':{
            'srchItemIdnfNo':'12345678','srchItemClsfNo':'55121718',
            'srchCtrtNo':'','srchCtrtChgOrd':'','srchCtrtItemSqno':''}}])

    def test_invalid_detail_response_is_not_an_empty_success(self):
        self.responses = [(200, b'{"ErrorCode":0,"dlPdctAtrbInfoL":{}}')]
        with patch('src.g2b_client.get_profile', return_value=self.profile), self.assertRaises(SourceError) as failure:
            enrich_product({'itemIdnfNo':'12345678','itemClsfNo':'55121718'})
        self.assertEqual(failure.exception.code,'invalid_detail_response')
