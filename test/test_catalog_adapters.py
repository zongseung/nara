import os
import unittest
from unittest.mock import patch

from src.catalog_client import diagnostic_profile, require_ready
from core.catalog_sources import SourceError


class AdapterTests(unittest.TestCase):
    def test_catalog_without_credentials_is_explicit(self):
        # Given no service key; when gated; then no HTTP or fake empty result.
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(SourceError) as caught,
        ):
            require_ready(diagnostic_profile("catalog-goods"))
        self.assertEqual(caught.exception.code, "requires_credentials")

    def test_credentials_do_not_claim_classification_or_pagination_validation(self):
        # Given credentials alone; when gated; then unknown contracts remain blocked.
        with (
            patch.dict(os.environ, {"DATA_GO_KR_SERVICE_KEY": "never-log-this"}),
            self.assertRaises(SourceError) as caught,
        ):
            require_ready(diagnostic_profile("catalog"))
        self.assertEqual(caught.exception.code, "requires_contract_validation")
        self.assertNotIn("never-log-this", str(caught.exception))

    def test_digital_main_is_distinct_and_still_unvalidated(self):
        # Given observed digital scope; when inspected; then main/all filters stay separate.
        main, all_items = (
            diagnostic_profile("digital-main"),
            diagnostic_profile("digital"),
        )
        self.assertEqual(main.purchase_method, "본010001")
        self.assertNotEqual(main.profile_hash, all_items.profile_hash)
        with self.assertRaises(SourceError):
            require_ready(main)

    def test_unknown_sources_are_not_empty_success(self):
        # Given unsupported sources; when gated; then explicit unsupported errors.
        for scope in ("venture", "innovation", "service", "contract-history"):
            with self.subTest(scope=scope), self.assertRaises(SourceError) as caught:
                require_ready(diagnostic_profile(scope))
            self.assertEqual(caught.exception.code, "unsupported_scope")

    def test_digital_profile_matches_captured_endpoint_target_and_main_filter(self):
        # Given the actual captured UI request, without assuming it proves pagination.
        import json
        from pathlib import Path

        fixture = Path(__file__).parent / "fixtures" / "source_contracts" / "digital_requests.json"
        captured = json.loads(fixture.read_text(encoding="utf-8"))
        record = next(
            r
            for r in captured["requests"]
            if r["path"].endswith("newDigitalUntySrchApi.do")
        )
        # When comparing the diagnostic profile; then only captured dimensions match.
        profile = diagnostic_profile("digital-main")
        self.assertTrue(profile.endpoint.endswith(record["path"]))
        self.assertEqual(profile.target, record["payload"]["searchVO"]["target"])
        self.assertEqual(
            profile.purchase_method, record["payload"]["searchVO"]["prchsMthdSeCd"]
        )
        self.assertEqual(profile.validation_status, "unvalidated")


class DiagnosticTransportTests(unittest.TestCase):
    def test_network_source_uses_one_limiter_for_every_retry(self):
        # Given the real shared transport against a local HTTP server returning transient failures.
        from src.catalog_client import NetworkSource
        from core.catalog_sources import SearchQuery
        from src.transport import RequestLimiter
        from test.test_catalog_client import WireTests

        wire = WireTests()
        wire.setUp()
        try:
            wire.responses = [
                (503, b"busy"),
                (503, b"busy"),
                (200, b'{"totalSize":0,"rsltList":[]}'),
            ]
            delays = []
            source = NetworkSource(wire.profile)
            source.limiter = RequestLimiter(
                rate=1, clock=lambda: 0, sleep=delays.append, jitter=lambda: 0
            )
            # When one logical page request succeeds after two retries.
            page = source.fetch(SearchQuery())
            # Then only three HTTP attempts occur and the shared limiter handles rate/backoff.
            self.assertEqual((page.attempts, len(wire.requests)), (3, 3))
            self.assertEqual(delays, [1, 1, 3, 1])
        finally:
            wire.tearDown()
