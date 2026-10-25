import unittest
from unittest.mock import patch

from core.comparison import CandidatePage, compare, compatible
from core.legacy_matching import extract


def product(identity, use, option=''):
    return extract({'itemIdnfNo': identity, 'itemIdnfNm': '안내판, MODEL01, 600×150mm',
        'mnftrEtpsNm': identity, 'ctrtUprc': '200', 'pdctAtrbCdDtlNm': f'재질:목재,용도:{use},지지대수:2개',
        '_detail_attributes': [{'atrbnm': '옵션/기타', 'atrbvalue': option}]})


class ComparisonOptionsTests(unittest.TestCase):
    def test_recording_mode_skips_supports_but_keeps_other_constraints(self):
        ours = dict(product('ours', ''), 지지대수=None)
        candidate = product('other', '')
        self.assertTrue(compatible(ours, candidate))
        self.assertTrue(compatible(ours, dict(candidate, 속성충돌=('지지대수',))))
        self.assertFalse(compatible(ours, dict(candidate, 규격=(800, 150))))
        self.assertTrue(compatible(ours, dict(candidate, 속성충돌=('표시면수',))))

    def test_direction_mismatch_and_unknown_are_allowed_until_selected(self):
        ours = product('ours', '1방향안내')
        for use in ('2방향안내', '종합안내', ''):
            with self.subTest(use=use):
                candidate = product('candidate', use)
                self.assertTrue(compatible(ours, candidate))
                self.assertFalse(compatible(ours, candidate, same_direction=True))
        self.assertTrue(compatible(ours, product('same', '1방향안내'), same_direction=True))

    @patch('core.comparison.CHECK_SUPPORTS', True)
    def test_optional_direction_conflict_does_not_hide_physical_conflicts(self):
        ours = product('ours', '1방향안내')
        mixed = product('mixed', '1방향/2방향')
        self.assertIn('방향수', mixed['속성충돌'])
        self.assertTrue(compatible(ours, mixed))
        self.assertFalse(compatible(ours, mixed, same_direction=True))
        self.assertFalse(compatible(ours, dict(mixed, 속성충돌=('방향수', '지지대수'))))
        self.assertFalse(compatible(ours, dict(mixed, 지지대수=1)))
        self.assertFalse(compatible(ours, dict(mixed, 규격=(800, 150))))

    def test_search_does_not_require_use_or_spend_detail_budget_on_optional_fields(self):
        ours = product('ours', '1방향안내')
        ours['가격'] = 100
        rows = [product('two', '2방향안내')['_raw'], product('unknown', '')['_raw']]
        searches = []
        class Provider:
            def search(self, keyword, lower='', upper='', limit=500):
                searches.append(keyword)
                return CandidatePage(tuple(rows), len(rows), False, None)
            def enrich(self, raw, *, required=()):
                if raw['itemIdnfNo'] != 'ours' and '방향수' not in required:
                    raise AssertionError('optional purpose triggered detail lookup')
                return raw
        _, candidates = compare(ours, Provider())
        self.assertEqual(searches, ['안내판'])
        self.assertEqual({item['식별번호'] for item in candidates}, {'two', 'unknown'})
        _, strict = compare(ours, Provider(), same_direction=True)
        self.assertEqual(strict, [])

    def test_review_tiers_fill_gaps_but_full_matches_take_priority(self):
        def raw(identity, price, face='양면', category='5512171801', material='목재'):
            return {'itemIdnfNo': identity, 'mnftrEtpsNm': identity, 'ctrtUprc': str(price),
                'itemIdnfNm': f'안내판, MODEL01, 600×150mm, {face}', 'dtlsPrnmNo': category,
                'pdctAtrbCdDtlNm': f'재질:{material},용도:종합안내'}
        ours = extract(raw('ours', 100))
        rows = [raw('alternative', 105, '단면'), raw('unknown', 110, ''),
                raw('complete', 150), raw('wrong-category', 115, category='other'),
                raw('wrong-material', 116, material='알루미늄'), raw('cheap', 90)]
        class Provider:
            def search(self, *args):
                return CandidatePage(tuple(rows), 800, True, None)
            def enrich(self, raw, *, required=()):
                return raw
        reference, selected = compare(ours, Provider())
        self.assertEqual([(c['업체'], c['비교구분']) for c in selected],
            [('alternative', '대안 후보'), ('unknown', '확인 필요'), ('complete', '조건 충족')])
        self.assertIn('표시면수 차이: 2 → 1', selected[0]['확인사항'])
        self.assertIn('표시면수 미확인', selected[1]['확인사항'])
        self.assertEqual(reference['후보진단']['탈락: 품목 분류'], 1)
        self.assertEqual(reference['후보진단']['탈락: 재질'], 1)
        self.assertEqual(reference['후보진단']['탈락: 가격'], 1)
        self.assertEqual(reference['후보진단']['조회범위제한'], 1)
        _, best = compare(ours, Provider(), top_n=1)
        self.assertEqual(best[0]['업체'], 'complete')

    def test_detail_budget_does_not_drop_candidates_with_only_soft_fields_missing(self):
        ours = product('ours', '종합안내', '양면')
        ours['가격'] = 100
        rows = [product(str(i), '종합안내')['_raw'] for i in range(30)]
        class Provider:
            def search(self, *args):
                return CandidatePage(tuple(rows), 30, False, None)
            def enrich(self, raw, *, required=()):
                return raw
        reference, selected = compare(ours, Provider(), top_n=3)
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(c['비교구분'] == '확인 필요' for c in selected))
        self.assertEqual(reference['후보진단']['상세조회'], 24)
        self.assertEqual(reference['후보진단']['상세조회 한도 초과'], 6)
        self.assertEqual(reference['후보진단']['확인 필요'], 30)

    def test_unknown_material_can_be_reviewed_and_enriched_price_is_rechecked(self):
        ours = product('ours', '종합안내', '양면')
        ours['가격'] = 100
        unknown = product('unknown', '종합안내')['_raw']
        unknown['pdctAtrbCdDtlNm'] = '용도:종합안내'
        changed = product('changed', '종합안내')['_raw']
        class Provider:
            def search(self, *args):
                return CandidatePage((unknown, changed), 2, False, None)
            def enrich(self, raw, *, required=()):
                return dict(raw, ctrtUprc='90') if raw['itemIdnfNo'] == 'changed' else raw
        reference, selected = compare(ours, Provider())
        self.assertEqual([c['업체'] for c in selected], ['unknown'])
        self.assertEqual(selected[0]['비교구분'], '확인 필요')
        self.assertIn('재질원문 미확인', selected[0]['확인사항'])
        self.assertEqual(reference['후보진단']['탈락: 가격'], 1)
