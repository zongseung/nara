import unittest

from core.catalog_sources import JsonValue
from core.comparison import CandidatePage, compare, compatible
from core.legacy_matching import (
    direction_count,
    extract,
    missing_product_fields,
    norm_material,
    parse_dims,
    score,
)
from core.matching.normalization import material_rank


class LegacyMatchingTests(unittest.TestCase):
    def test_compare_runs_on_an_injected_provider_without_network(self):
        class Provider:
            def __init__(self, rows):
                self.rows, self.searches = rows, []

            def search(self, keyword, lower='', upper='', limit=500):
                self.searches.append((keyword, lower, upper))
                return CandidatePage(tuple(self.rows), len(self.rows), False, None)

            def enrich(self, raw, *, required=()):
                return raw

        def row(identity, company, price, name='안내판, 600×150mm, 지지대 2개'):
            return {'itemIdnfNo':identity, 'itemIdnfNm':name, 'mnftrEtpsNm':company, 'ctrtUprc':str(price),
                    '_detail_attributes':[{'atrbnm':'재질', 'atrbvalue':'목재'}]}

        ours = extract(dict(row('ours', '우리', 100), _detail_attributes=[
            {'atrbnm':'재질', 'atrbvalue':'목재'}, {'atrbnm':'모델명', 'atrbvalue':'GK-ST08'}]))
        provider = Provider([row('cheap', '경쟁A', 90), row('b', '경쟁B', 130), row('a', '(주)경쟁A', 120),
                             row('a2', '경쟁A', 125), row('self', '우리', 140),
                             row('wide', '경쟁C', 110, '안내판, 700×150mm, 지지대 2개')])
        our, candidates = compare(ours, provider, excluded=['우리'])
        self.assertEqual(our['모델'], 'GK-ST08')
        self.assertEqual([c['식별번호'] for c in candidates], ['a', 'b'])
        self.assertEqual(provider.searches, [('안내판', '100', '')])

        provider.searches.clear()
        _, approx = compare(ours, provider, exact_price=True, fallback_band=0.3, excluded=['우리'])
        self.assertEqual([(c['식별번호'], c['근사']) for c in approx], [('a', True), ('b', True)])
        self.assertEqual(provider.searches, [('안내판', '100', '100'), ('안내판', '70.0', '130.0')])

        provider.searches.clear()
        held, none = compare(extract(row('x', '우리', 100, '안내판, 600×150mm')), provider)
        self.assertEqual((held['비교보류사유'], none, provider.searches), ('비교 보류: 지지대수 미확인', [], []))

    def test_direction_and_faces_use_all_three_sources_and_reject_conflicts(self):
        cases = (
            ('방향안내', '2방향/양면', '', 2, 2, ()),
            ('1방향안내/양면', '', '', 1, 2, ()),
            ('방향안내', '단면2방향', '', 2, 1, ()),
            ('방향안내', '양면2방향', '', 2, 2, ()),
            ('1방향안내', '2방향/양면', '', None, 2, ('방향수',)),
            ('1방향/2방향', '', '', None, None, ('방향수',)),
            ('단방향/양방향', '', '', None, None, ('방향수',)),
            ('1방향안내/양면', '단면', '', 1, None, ('표시면수',)),
            ('방향안내', '２ 방향/양 면', '2방향, 양면', 2, 2, ()),
            ('방향안내', '양면 아님', '', None, None, ()),
            ('방향안내', '2방향 미지원', '', None, None, ()),
            ('방향안내', '양면테이프 부착/양면 테이프/단면도 참조', '', None, None, ()),
            ('방향안내', '', 'MODEL12 2방향 양면', 2, 2, ()),
        )
        for use, option, name, directions, faces, conflicts in cases:
            with self.subTest(use=use, option=option, name=name):
                raw = {'itemIdnfNm': f'안내판, AB12, 600×150mm, {name}',
                       'pdctAtrbNm': 'x$용도$U9|x$옵션/기타$O7',
                       'pdctAtrbCdDtl': f'U9token${use}|O7token${option}'}
                item = extract(raw)
                self.assertEqual(item['방향수'], directions)
                self.assertEqual(item['표시면수'], faces)
                self.assertEqual(item['속성충돌'], conflicts)
                self.assertEqual(item['용도'], use)
                self.assertEqual(item['속성']['옵션/기타'], option)
                self.assertIs(item['_raw'], raw)

    def test_direction_count_never_picks_first_of_conflicting_values(self):
        for text, expected in (('1방향/2방향', None), ('일방향/양방향', None),
                               ('단방향/1방향', 1), ('２ 방향', 2), ('0방향', None),
                               ('1.5방향', None), ('A2방향', None), ('2방향 아님', None),
                               ('MODEL12 2방향', 2), ('단 방향', 1)):
            with self.subTest(text=text):
                self.assertEqual(direction_count(text), expected)

    def test_purpose_formatting_is_shared_by_compatibility_and_score(self):
        base = extract({'itemIdnfNm': '안내판, AB12, 600×150mm',
                        'pdctAtrbCdDtlNm': '용도:1방향안내,지지대수:0개'})
        for use in ('방향안내/1방향', '방향안내／１ 방향', '방향안내&#47;1방향', '일방향안내'):
            with self.subTest(use=use):
                candidate = dict(base, 용도=use)
                self.assertTrue(compatible(base, candidate))
                self.assertEqual(score(base, candidate)[1]['용도'], 1.0)
                self.assertEqual(candidate['용도'], use)
        for use in ('2방향안내', '1방향안내 아님', '공원1방향안내', '1방향안내/입간판형',
                    '1방향안내/2층', '방향안내', '종합안내', ''):
            with self.subTest(different=use):
                self.assertFalse(compatible(base, dict(base, 용도=use)))

    def test_options_do_not_invent_supports_or_replace_material_size_or_price(self):
        for option, supports in (('기둥미포함', 0), ('', None), ('75각 2개', None),
                                 ('기초포함', None), ('주기둥 STS304', None)):
            with self.subTest(option=option):
                raw: dict[str, JsonValue] = {'itemIdnfNm': '안내판, AB12, 600×150mm', 'ctrtUprc': '1430000',
                       'dscntAplcnUprc': 1287000, 'srchUprc': '1287000',
                       '_detail_attributes': [
                           {'atrbnm': '재질', 'atrbvalue': '목재'},
                           {'atrbnm': '옵션/기타', 'atrbvalue': option + '/전체크기2000×2000mm'}]}
                item = extract(raw)
                self.assertEqual(item['지지대수'], supports)
                self.assertEqual(item['재질원문'], '목재')
                self.assertEqual(item['규격'], (600, 150))
                self.assertEqual(item['가격'], 1430000)

    def test_material_and_dimensions_keep_normalization(self):
        self.assertEqual(norm_material("SUS"), "스테인리스")
        self.assertEqual(parse_dims("500x450mm"), (500, 450))
        self.assertEqual(norm_material(""), "")
        self.assertIsNone(parse_dims(""))

    def test_extract_preserves_raw_and_decodes_fields(self):
        raw = {
            "itemIdnfNm": "안내판, AB12, SUS 500x450mm",
            "mnftrEtpsNm": "A&amp;B",
            "pdctAtrbCdDtlNm": "용도:도로명판걸이",
            "ctrtUprc": "120.9",
        }
        result = extract(raw)
        self.assertIs(result["_raw"], raw)
        self.assertEqual(result["업체"], "A&B")
        self.assertEqual(result["품명"], "안내판")
        self.assertEqual(result["규격"], (500, 450))
        self.assertEqual(result["재질"], "스테인리스")
        self.assertEqual(result["용도"], "도로명판걸이")
        self.assertEqual(str(result["가격"]), "120.9")
        self.assertEqual(result["모델"], "AB12")

    def test_score_preserves_weights_and_dimension_tolerance(self):
        ours = {"재질": "SUS", "규격": (100, 100), "용도": "안내판", "가격": 100}
        candidate = {"재질": "목재", "규격": (110, 100), "용도": "안내판", "가격": 50}
        total, parts = score(ours, candidate)
        self.assertEqual(parts, {"재질": 0.0, "규격": 1.0, "용도": 1.0, "가격": 0.5})
        self.assertAlmostEqual(total, 0.375)

    def test_structured_attributes_preserve_material_direction_and_units(self):
        # Given arbitrary source attribute codes and separate dimensions.
        raw = {"itemIdnfNm": "안내판, AB12", "ctrtUprc": "9007199254740993.25",
               "pdctAtrbNm": "x$재질$M9|x$용도$U7|x$표지크기&#40;가로&#41;$W4|x$표지크기(세로)$H2|x$옵션/기타$O5",
               "pdctAtrbCdDtl": "M9혼합$알루미늄 A5052-H32/스테인리스강|U7둘$2방향안내|W4폭$60cm|H2높이$150mm|O5면$양면"}
        # When reading source-defined labels rather than fixed code positions.
        item = extract(raw)
        # Then all comparison evidence remains distinct and prices retain cents.
        self.assertEqual(item["규격"], (600, 150))
        self.assertEqual(item["방향수"], 2)
        self.assertEqual(item["표시면수"], 2)
        self.assertEqual(item["재질원문"], "알루미늄 A5052-H32/스테인리스강")
        self.assertEqual(str(item["가격"]), "9007199254740993.25")

    def test_material_matching_keeps_grades_and_mixed_components(self):
        # Given aliases, incompatible grades, and mixed or distinct materials.
        pairs = [("SUS304", "STS304", 3), ("SUS304", "STS316", 0),
                 ("알미늄", "알루미늄/스테인리스", 1), ("유리", "강화유리", 0),
                 ("구리", "황동", 0), ("HDPE", "PC", 0)]
        for left, right, expected in pairs:
            # When comparing complete material descriptions.
            with self.subTest(left=left, right=right):
                rank = material_rank(left, right)
                # Then aliases match without upgrading grades or losing mixtures.
                self.assertEqual(rank, expected)

    def test_detail_conflicts_remain_explicit(self):
        # Given conflicting search and detail evidence.
        raw: dict[str, JsonValue] = {"pdctAtrbNm": "x$재질$M", "pdctAtrbCdDtl": "M목$목재",
               "_detail_attributes": [{"atrbnm": "재질", "atrbvalue": "알루미늄", "atrboptnvalue": None}]}
        # When extracting the product.
        item = extract(raw)
        # Then the conflict is exposed instead of silently replacing the source.
        self.assertEqual(item["속성충돌"], ("재질",))
        self.assertEqual(item["재질원문"], "목재")

    def test_direction_and_faces_cannot_silently_override_conflicting_evidence(self):
        raw: dict[str, JsonValue] = {"itemIdnfNm":"안내판, 1방향안내, 양면", "pdctAtrbCdDtlNm":"용도:2방향안내",
               "_detail_attributes":[{"atrbnm":"옵션/기타","atrbvalue":"단면","atrboptnvalue":None}]}
        item = extract(raw)
        self.assertEqual(item['속성충돌'], ('방향수','표시면수'))

    def test_support_count_uses_explicit_evidence_and_keeps_absence_distinct_from_unknown(self):
        cases = [
            ('안내판, AB12', '지지대수', '2본', 2),
            ('안내판, AB12', '다리 개수', '1개', 1),
            ('안내판, AB12', '다리 개수', '2.0', 2),
            ('안내판, AB12', '지지대수', '0.0개', 0),
            ('안내판, AB12', '지지대수', '1.5개', None),
            ('안내판, AB12', '지지대', '없음', 0),
            ('안내판, AB12, 지주 2개', '재질', '목재', 2),
            ('안내판, AB12, 1주식', '재질', '목재', 1),
            ('안내판, AB12, 지지대 미포함', '재질', '목재', 0),
            ('안내판, AB12, 지지대 2개 미포함', '재질', '목재', 0),
            ('안내판, AB12, 양면', '지지대높이', '2000mm', None),
            ('안내판, AB12', '설치방식', '고정식', None),
        ]
        for name, label, value, expected in cases:
            with self.subTest(name=name, label=label, value=value):
                item = extract({'itemIdnfNm':name, 'pdctAtrbNm':f'x${label}$Q9',
                                'pdctAtrbCdDtl':f'Q9token${value}'})
                self.assertEqual(item.get('지지대수'), expected)

    def test_required_support_count_accepts_numeric_zero_from_detail(self):
        item = extract({'itemIdnfNm':'안내판, AB12', '_detail_attributes':[
            {'atrbnm':'지지대수', 'atrbvalue':0, 'atrboptnvalue':'개'}]})
        self.assertEqual(item.get('지지대수'), 0)
        self.assertNotIn('지지대수', missing_product_fields(item, ('지지대수',)))

    def test_unparsed_support_attribute_still_requires_detail_evidence(self):
        item = extract({'itemIdnfNm':'안내판, AB12', 'pdctAtrbCdDtlNm':'지지대수:미기재'})
        self.assertIn('지지대수', missing_product_fields(item, ('지지대수',)))

    def test_conflicting_support_counts_block_a_product(self):
        item = extract({'itemIdnfNm':'안내판, AB12, 지지대 2개',
                        'pdctAtrbCdDtlNm':'지지대수:1개'})
        self.assertIn('지지대수', item['속성충돌'])

    def test_specification_score_no_longer_gives_full_credit_to_35_percent_difference(self):
        _, parts = score({'규격':(100,100)}, {'규격':(135,100)})
        self.assertLess(parts['규격'], 1.0)
