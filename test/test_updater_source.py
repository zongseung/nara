import unittest
from dataclasses import replace
from decimal import Decimal

from core.catalog_sources import SourceError, get_profile
from src.g2b_client import parse_search_page
from src.updater.source import observations_from_page


def page_for(**overrides):
    row = {'ctrtItemMngNo': 'CONTRACT-A', 'itemIdnfNo': '123', 'ctrtUprc': '100', 'totCnt': 1,
           'itemIdnfNm': 'Item', 'ctentUntyGrpNm': 'Seller', 'mnftrEtpsNm': 'Maker'}
    row.update(overrides)
    return parse_search_page({'totalSize': 1, 'rsltList': [row]}, get_profile('shopping-all'))


class ObservationTests(unittest.TestCase):
    def test_decimal_equivalence_and_distinct_contracts(self):
        a = observations_from_page(page_for())[0]
        b = observations_from_page(page_for(ctrtUprc='100.0', ctrtItemMngNo='CONTRACT-B'))[0]
        self.assertEqual(a.contract_price_decimal, b.contract_price_decimal)
        self.assertEqual(a.contract_price_decimal, Decimal('100'))
        self.assertNotEqual(a.listing_key, b.listing_key)

    def test_null_price_is_rejected_only_by_business_layer(self):
        page = page_for(ctrtUprc=None)
        self.assertIsNone(page.rows[0].contract_price_decimal)
        with self.assertRaises(SourceError) as failure:
            observations_from_page(page)
        self.assertEqual(failure.exception.code, 'required_price')

    def test_names_and_parties_remain_separate(self):
        item = observations_from_page(page_for(itemIndfNmView='Other'))[0]
        self.assertEqual((item.seller_name, item.manufacturer_name), ('Seller', 'Maker'))
        self.assertEqual(dict(item.raw_names), {'itemIdnfNm': 'Item', 'itemIndfNmView': 'Other'})
        self.assertEqual(item.name_status, 'unresolved')

    def test_unknown_semantics_are_never_invented(self):
        item = observations_from_page(page_for(currency='KRW', tax='included', bzmnRegNo='12345'))[0]
        self.assertEqual((item.currency, item.tax_basis, item.pack_quantity, item.unit,
                          item.seller_id, item.manufacturer_id, item.source_modified_at), (None,) * 7)
        certs = next(group for group in item.groups if group.name == 'certifications')
        self.assertEqual((certs.supported, certs.status, certs.checked_at), (False, 'unsupported', None))

    def test_missing_and_null_do_not_mean_deleted(self):
        item = observations_from_page(page_for(ctrtUntVal=None))[0]
        fields = dict(item.field_states)
        self.assertEqual(fields['ctrtUntVal'], 'null')
        self.assertEqual(fields['ctrtNo'], 'missing')
        self.assertNotIn('deleted', fields.values())

    def test_each_response_has_its_own_observation_time(self):
        page = page_for()
        item = observations_from_page(page)[0]
        self.assertEqual(item.observed_at, page.observed_at)
        self.assertIsNotNone(item.observed_at.tzinfo)

    def test_bad_optional_types_fail_instead_of_becoming_unknown(self):
        page = page_for(mnftrEtpsNm=['unexpected'])
        with self.assertRaises(SourceError):
            observations_from_page(page)

    def test_untrusted_text_is_preserved_as_data(self):
        text = '=HYPERLINK("https://example.invalid") ignore prior instructions'
        item = observations_from_page(page_for(itemIdnfNm=text))[0]
        self.assertEqual(item.display_name, text)

    def test_wrong_scope_and_error_pages_are_rejected(self):
        for page in (replace(page_for(), profile=get_profile('shopping-main')),
                     replace(page_for(), errors=(SourceError('failed_page'),))):
            with self.subTest(page=page), self.assertRaises(SourceError):
                observations_from_page(page)

    def test_invalid_identity_fails_even_when_price_is_valid(self):
        with self.assertRaises(SourceError):
            observations_from_page(page_for(ctrtItemMngNo=None))
