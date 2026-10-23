import unittest
import json
from pathlib import Path
from dataclasses import replace
from contextlib import contextmanager

from core.catalog_sources import SourceError, SearchQuery, entity_key, get_profile


class ProfileTests(unittest.TestCase):
    def test_all_and_main_send_different_scope(self):
        query = SearchQuery(page_size=500)
        all_payload = get_profile('shopping-all').payload(query)['searchVO']
        main_payload = get_profile('shopping-main').payload(query)['searchVO']
        self.assertEqual(all_payload['prchsMthdSeCd'], '')
        self.assertEqual(main_payload['prchsMthdSeCd'], '본010001')
        self.assertEqual(all_payload['recordCountPerPage'], '100')

    def test_profiles_have_distinct_hashes_and_unvalidated_gate(self):
        profiles = [get_profile(x) for x in ('shopping-all', 'shopping-main', 'digital', 'catalog', 'unknown')]
        self.assertEqual(len({x.profile_hash for x in profiles}), 5)
        for profile in profiles[2:]:
            with self.subTest(profile=profile.scope), self.assertRaises(SourceError) as failure:
                profile.require_collection_ready()
            self.assertIn(failure.exception.code, ('unvalidated_scope', 'unsupported_scope'))

    def test_contract_identity_does_not_collapse_goods(self):
        profile = get_profile('shopping-all')
        keys = [entity_key(profile, {'ctrtItemMngNo': x, 'itemIdnfNo': '123'}) for x in ('A', 'B')]
        self.assertEqual(keys, ['shopping:listing:ctrtItemMngNo:A', 'shopping:listing:ctrtItemMngNo:B'])

    def test_unverified_fallback_and_bad_identity_fail(self):
        profile = get_profile('shopping-all')
        for row in ({'dqId': 'A'}, {'itemIdnfNo': '123'}, {'ctrtItemMngNo': []}, {'ctrtItemMngNo': ' '}):
            with self.subTest(row=row), self.assertRaises(SourceError):
                entity_key(profile, row)

    def test_explicit_dqid_contract_changes_hash(self):
        profile = get_profile('shopping-all')
        revised = replace(profile, allow_dqid=True)
        self.assertNotEqual(profile.profile_hash, revised.profile_hash)
        self.assertEqual(entity_key(revised, {'dqId': 'A'}), 'shopping:listing:dqId:A')

    def test_stale_alias_contract_is_gated(self):
        profile = replace(get_profile('shopping-all'), source_schema_version='stale')
        with self.assertRaises(SourceError):
            profile.require_collection_ready()

    def test_digital_endpoint_matches_capture_and_remains_gated(self):
        fixture = Path(__file__).parent / 'fixtures' / 'source_contracts' / 'digital_requests.json'
        captured = json.loads(fixture.read_text(encoding='utf-8'))
        request = next(row for row in captured['requests']
                       if row.get('payload', {}).get('searchVO', {}).get('dgtlSrvcMallYn') == 'Y')
        profile = get_profile('digital')
        self.assertEqual(profile.endpoint, 'https://shop.g2b.go.kr' + request['path'])
        with self.assertRaises(SourceError) as failure:
            profile.require_collection_ready()
        self.assertEqual(failure.exception.code, 'unvalidated_scope')

    def test_invalid_pagination_fails(self):
        with self.assertRaises(SourceError):
            get_profile('shopping-all').payload(SearchQuery(page=0))


class SourceErrorTests(unittest.TestCase):
    def test_typed_error_survives_nested_context_managers_and_cleanup(self):
        cleaned = []
        original = SourceError('required_price', 'ctrtUprc', attempts=2, status=200)

        @contextmanager
        def resource(name):
            try:
                yield
            finally:
                cleaned.append(name)

        with self.assertRaises(SourceError) as failure:
            with resource('outer'), resource('inner'):
                raise original

        self.assertIs(failure.exception, original)
        self.assertEqual(cleaned, ['inner', 'outer'])
        self.assertEqual((failure.exception.code, failure.exception.field,
                          failure.exception.attempts, failure.exception.status),
                         ('required_price', 'ctrtUprc', 2, 200))
