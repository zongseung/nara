import unittest
from decimal import Decimal
from core.matching.contracts import (
    AttributeEvidence,
    EvidenceState,
    ContractError,
    Purpose,
    InputItem,
    PriceBasis,
)


class ContractTests(unittest.TestCase):
    def test_decimal_evidence_roundtrip(self):
        value = AttributeEvidence(
            (Decimal("200"), Decimal("300.25")),
            "mm",
            "spec",
            "20x30.025cm",
            EvidenceState.KNOWN,
        )
        self.assertEqual(AttributeEvidence.from_json(value.to_json()), value)

    def test_missing_cannot_carry_value(self):
        with self.assertRaises(ContractError):
            AttributeEvidence(Decimal("0"), "mm", "spec", "", EvidenceState.MISSING)

    def test_unknown_purpose_rejected(self):
        with self.assertRaises(ValueError):
            Purpose("guess")

    def test_price_missing_is_not_zero(self):
        self.assertIsNone(PriceBasis().amount)

    def test_required_request_fields(self):
        with self.assertRaises(ContractError):
            InputItem("", Purpose.EXACT_IDENTITY, "", "name", "")

    def test_nonfinite_attribute_rejected(self):
        with self.assertRaises(ContractError):
            AttributeEvidence(Decimal("NaN"), "mm", "spec", "NaN", EvidenceState.KNOWN)

    def test_input_contract_roundtrip(self):
        from core.matching.contracts import dumps_contract, loads_contract

        for purpose in Purpose:
            item = InputItem(
                "row",
                purpose,
                "fixture",
                "name",
                "20x30cm",
                price=PriceBasis(Decimal("1200.25")),
            )
            self.assertEqual(loads_contract(dumps_contract(item)), item)

    def test_candidate_timestamps_roundtrip(self):
        from datetime import datetime, timezone, date
        from core.matching.contracts import Candidate, dumps_contract, loads_contract

        candidate = Candidate(
            "listing",
            "fixture",
            "name",
            contract_until=date(2026, 10, 23),
            group_checked_at=(("item", datetime(2026, 10, 23, tzinfo=timezone.utc)),),
        )
        self.assertEqual(loads_contract(dumps_contract(candidate)), candidate)

    def test_nested_decimal_evidence_preserves_type(self):
        from core.matching.contracts import dumps_contract, loads_contract

        a = AttributeEvidence(
            Decimal("3.25"), "L", "capacity", "3.25L", EvidenceState.KNOWN
        )
        b = AttributeEvidence(
            (Decimal("20"), Decimal("30")),
            "mm",
            "dimensions",
            "20x30mm",
            EvidenceState.KNOWN,
        )
        item = InputItem(
            "r",
            Purpose.FUNCTIONAL_SUBSTITUTE,
            "fixture",
            "name",
            "spec",
            attributes=(("capacity", a), ("dimensions", b)),
        )
        decoded = loads_contract(dumps_contract(item))
        self.assertEqual(decoded, item)
        self.assertIsInstance(dict(decoded.attributes)["capacity"].value, Decimal)

    def test_nested_nonfinite_value_rejected(self):
        with self.assertRaises(ContractError):
            AttributeEvidence(
                ((Decimal("NaN"),),), "mm", "spec", "bad", EvidenceState.KNOWN
            )

    def test_wrong_schema_and_enum_rejected_at_boundary(self):
        import json
        from core.matching.contracts import dumps_contract, loads_contract

        data = json.loads(
            dumps_contract(
                InputItem("r", Purpose.EXACT_IDENTITY, "fixture", "name", "spec")
            )
        )
        data["payload"]["purpose"] = "guess"
        with self.assertRaises(ContractError):
            loads_contract(json.dumps(data))
        data["schema"] = "matching/999"
        with self.assertRaises(ContractError):
            loads_contract(json.dumps(data))

    def test_numeric_string_not_decimal(self):
        from core.matching.contracts import dumps_contract, loads_contract

        value = AttributeEvidence("3.25", "text", "model", "3.25", EvidenceState.KNOWN)
        self.assertEqual(loads_contract(dumps_contract(value)), value)

    def test_decision_statuses_roundtrip(self):
        from core.matching.contracts import MatchDecision,DecisionStatus,ReasonCode,dumps_contract,loads_contract
        for status in DecisionStatus:
            decision=MatchDecision('row',status,(ReasonCode.MISSING_REQUIRED_ATTRIBUTE,))
            self.assertEqual(loads_contract(dumps_contract(decision)),decision)
