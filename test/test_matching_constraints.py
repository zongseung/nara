import unittest
from datetime import datetime, date, timezone
from dataclasses import replace
from decimal import Decimal
from core.matching.contracts import *
from core.matching.normalization import normalize_dimensions, normalize_material
from core.matching.policy import load_policy
from core.matching.constraints import evaluate_constraints, aggregate_status
from test.test_matching_policy import policy_data

NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)


def pair(spec="200x300mm"):
    attrs = (("dimensions", normalize_dimensions(spec)),)
    item = InputItem(
        "r",
        Purpose.FUNCTIONAL_SUBSTITUTE,
        "fixture",
        "item",
        spec,
        attributes=attrs,
        item_kind="main",
    )
    candidate = Candidate(
        "c",
        "fixture",
        "item",
        attributes=attrs,
        item_kind="main",
        contract_until=date(2026, 10, 25),
        latest_contract=True,
        group_checked_at=(("contract", NOW),),
    )
    return item, candidate


class ConstraintTests(unittest.TestCase):
    def check(self, item, candidate, **changes):
        return aggregate_status(
            evaluate_constraints(
                item, candidate, load_policy(policy_data(**changes)), now=NOW
            )
        )

    def test_equal_passes(self):
        self.assertEqual(self.check(*pair()), ConstraintStatus.PASS)

    def test_missing_dimensions_unknown(self):
        self.assertEqual(self.check(*pair("200x300")), ConstraintStatus.UNKNOWN)

    def test_thirty_five_percent_is_failure(self):
        item, candidate = pair()
        candidate = replace(
            candidate, attributes=(("dimensions", normalize_dimensions("270x405mm")),)
        )
        self.assertEqual(self.check(item, candidate), ConstraintStatus.FAIL)

    def test_kst_expired_at_midnight(self):
        item, candidate = pair()
        candidate = replace(candidate, contract_until=date(2026, 10, 23))
        self.assertEqual(self.check(item, candidate), ConstraintStatus.FAIL)

    def test_stale_unknown(self):
        item, candidate = pair()
        candidate = replace(
            candidate,
            group_checked_at=(
                ("contract", datetime(2026, 10, 20, tzinfo=timezone.utc)),
            ),
        )
        self.assertEqual(self.check(item, candidate), ConstraintStatus.UNKNOWN)

    def test_price_basis_unknown(self):
        item, candidate = pair()
        item = replace(
            item,
            purpose=Purpose.SAME_PRICE_COMPETITOR,
            price=PriceBasis(Decimal("100")),
        )
        candidate = replace(
            candidate, price=PriceBasis(Decimal("100")), seller_id="other"
        )
        self.assertEqual(
            self.check(item, candidate, purpose="same_price_competitor"),
            ConstraintStatus.UNKNOWN,
        )

    def test_rotation_explicit(self):
        item, candidate = pair()
        candidate = replace(
            candidate, attributes=(("dimensions", normalize_dimensions("300x200mm")),)
        )
        self.assertEqual(
            self.check(item, candidate, allow_rotation=True), ConstraintStatus.PASS
        )

    def test_model_conflict_fails_identity(self):
        item, candidate = pair()
        item = replace(
            item,
            purpose=Purpose.EXACT_IDENTITY,
            goods_id="001",
            goods_id_verified=True,
            model="A",
        )
        candidate = replace(
            candidate, goods_id="001", goods_id_verified=True, model="B"
        )
        self.assertEqual(
            self.check(item, candidate, purpose="exact_identity"), ConstraintStatus.FAIL
        )

    def test_missing_identity_spec_cannot_pass_on_goods_id(self):
        item, candidate = pair()
        item = replace(
            item, purpose=Purpose.EXACT_IDENTITY, goods_id="001", goods_id_verified=True
        )
        candidate = replace(
            candidate, goods_id="001", goods_id_verified=True, attributes=()
        )
        self.assertEqual(
            self.check(item, candidate, purpose="exact_identity", rules=[]),
            ConstraintStatus.UNKNOWN,
        )

    def test_price_kind_mismatch_fails(self):
        item, candidate = pair()
        item = replace(
            item,
            purpose=Purpose.SAME_PRICE_COMPETITOR,
            seller_id="seller",
            price=PriceBasis(
                Decimal("100"), "desired_price", "KRW", "included", "EA", Decimal(1)
            ),
        )
        candidate = replace(
            candidate,
            seller_id="other",
            price=PriceBasis(
                Decimal("100"),
                "contract_unit_price",
                "KRW",
                "included",
                "EA",
                Decimal(1),
            ),
        )
        self.assertEqual(
            self.check(item, candidate, purpose="same_price_competitor"),
            ConstraintStatus.FAIL,
        )

    def test_required_certificate_missing_unknown(self):
        self.assertEqual(
            self.check(*pair(), required_certificates=["fixture-safety"]),
            ConstraintStatus.UNKNOWN,
        )

    def test_threshold_and_membership_operators(self):
        for operator,threshold,value,expected in (
            ('min','10',Decimal('10'),ConstraintStatus.PASS),
            ('min','10',Decimal('9.99'),ConstraintStatus.FAIL),
            ('max','10',Decimal('10.01'),ConstraintStatus.FAIL),
            ('range',['10','20'],Decimal('20'),ConstraintStatus.PASS),
            ('range',['10','20'],Decimal('20.01'),ConstraintStatus.FAIL),
            ('set',['A','B'],'B',ConstraintStatus.PASS),
            ('set',['A','B'],'C',ConstraintStatus.FAIL),
        ):
            with self.subTest(operator=operator,value=value):
                unit='text' if operator=='set' else 'L'
                item,candidate=pair()
                evidence=AttributeEvidence(value,unit,'capacity',str(value),EvidenceState.KNOWN)
                candidate=replace(candidate,attributes=(('capacity',evidence),))
                rule=dict(field='capacity',operator=operator,unit=unit,required=True,tolerance='0',value=threshold)
                self.assertEqual(self.check(item,candidate,rules=[rule]),expected)

    def test_same_price_comparable_other_seller_passes(self):
        item,candidate=pair()
        price=PriceBasis(Decimal('100'),'contract_unit_price','KRW','included','EA',Decimal('1'))
        item=replace(item,purpose=Purpose.SAME_PRICE_COMPETITOR,seller_id='seller',price=price)
        candidate=replace(candidate,seller_id='other',price=price)
        self.assertEqual(self.check(item,candidate,purpose='same_price_competitor'),ConstraintStatus.PASS)

    def test_explicit_seller_exclusion(self):
        item,candidate=pair(); item=replace(item,excluded_seller_ids=('excluded',));candidate=replace(candidate,seller_id='excluded')
        self.assertEqual(self.check(item,candidate),ConstraintStatus.FAIL)
