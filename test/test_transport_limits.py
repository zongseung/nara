import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests
from core.catalog_sources import SearchQuery, SourceError, get_profile
from src.transport import RequestLimiter
from src.g2b_client import fetch_search_page
from src.updater.source import ShoppingSource


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def limiter(self, rate=1):
        return RequestLimiter(rate, clock=self.read, sleep=self.sleep,
                              wall_clock=lambda: 1000.0, jitter=lambda: .5)


class LimiterTests(unittest.TestCase):
    def test_threads_share_one_request_start_budget(self):
        clock = Clock()
        limiter = clock.limiter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: limiter.before_request(), range(4)))
        self.assertEqual(clock.waits, [1, 1, 1])

    def test_retry_after_seconds_caps_at_120(self):
        clock = Clock()
        clock.limiter().backoff(1, '999999')
        self.assertEqual(clock.waits, [120])

    def test_retry_after_date_uses_injected_wall_clock(self):
        clock = Clock()
        header = format_datetime(datetime.fromtimestamp(1012, timezone.utc), usegmt=True)
        clock.limiter().backoff(1, header)
        self.assertEqual(clock.waits, [12])

    def test_invalid_retry_after_falls_back_to_bounded_jitter(self):
        for header in ('garbage', '-10', '9' * 5000, None):
            with self.subTest(header=header):
                clock = Clock()
                limiter = clock.limiter()
                limiter.backoff(1, header)
                limiter.backoff(2, header)
                self.assertEqual(clock.waits, [1.5, 3.5])

    def test_invalid_rate_is_typed(self):
        for rate in (0, -1, float('nan'), float('inf')):
            with self.subTest(rate=rate), self.assertRaises(SourceError):
                RequestLimiter(rate)


class WireLimitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = self.clock.limiter()
        self.starts = []
        self.responses = [(429, '999999'), (503, None), (200, None)]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                owner.starts.append(owner.clock.now)
                status, retry_after = owner.responses[min(len(owner.starts) - 1, len(owner.responses) - 1)]
                body = b'{"totalSize":0,"rsltList":[]}'
                self.send_response(status)
                if retry_after:
                    self.send_header('Retry-After', retry_after)
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
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_each_real_attempt_obeys_retry_after_and_backoff(self):
        page = fetch_search_page(SearchQuery(), self.profile, limiter=self.limiter)
        self.assertEqual((page.attempts, self.starts, self.clock.waits), (3, [0, 120, 123.5], [120, 3.5]))

    def test_two_calls_share_rate_budget_including_first_attempts(self):
        self.responses = [(200, None)]
        for _ in range(2):
            fetch_search_page(SearchQuery(), self.profile, limiter=self.limiter)
        self.assertEqual(self.starts, [0, 1])

    def test_auth_failure_has_no_retry_or_backoff(self):
        self.responses = [(401, '120')]
        with self.assertRaises(SourceError) as failure:
            fetch_search_page(SearchQuery(), self.profile, limiter=self.limiter)
        self.assertEqual((failure.exception.attempts, self.starts, self.clock.waits), (1, [0], []))

    def test_shopping_source_passes_limiter_and_split_timeout(self):
        with patch('src.updater.source.get_profile', return_value=self.profile):
            with requests.Session() as session:
                original = session.post
                with patch.object(session, 'post', wraps=original) as posted:
                    source = ShoppingSource(session=session, limiter=self.limiter)
                    source.fetch(SearchQuery())
        self.assertEqual((self.starts, posted.call_args.kwargs['timeout']), ([0, 120, 123.5], (10, 35)))
