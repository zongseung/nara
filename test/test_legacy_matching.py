import unittest

from core.catalog_sources import JsonValue
from core.legacy_matching import (
    extract,
    missing_product_fields,
    norm_material,
    parse_dims,
    score,
)
from core.matching.normalization import material_rank


class LegacyMatchingTests(unittest.TestCase):
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
