"""Mandatory conditions, independent of similarity/model scores."""

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.matching.contracts import (
    AttributeEvidence,
    AttributeValue,
    Candidate,
    ConstraintResult,
    ConstraintStatus,
    ContractError,
    EvidenceState,
    InputItem,
    Purpose,
    ReasonCode,
)
from core.matching.policy import AttributeRule, Operator, Policy


def aggregate_status(results: Iterable[ConstraintResult]) -> ConstraintStatus:
    statuses = {r.status for r in results}
    if ConstraintStatus.FAIL in statuses:
        return ConstraintStatus.FAIL
    if ConstraintStatus.UNKNOWN in statuses or not statuses:
        return ConstraintStatus.UNKNOWN
    return ConstraintStatus.PASS


def _result(
    field: str,
    passed: bool | None,
    expected: AttributeValue = None,
    actual: AttributeValue = None,
    reason: ReasonCode | None = None,
) -> ConstraintResult:
    status = (
        ConstraintStatus.UNKNOWN
        if passed is None
        else ConstraintStatus.PASS
        if passed
        else ConstraintStatus.FAIL
    )
    code = reason or (
        ReasonCode.MISSING_REQUIRED_ATTRIBUTE
        if passed is None
        else ReasonCode.MATCH
        if passed
        else ReasonCode.MISMATCH
    )
    return ConstraintResult(field, status, code, expected, actual)


def _attribute(
    rule: AttributeRule,
    requested: AttributeEvidence | None,
    actual: AttributeEvidence | None,
    rotation: bool,
) -> ConstraintResult:
    if actual is None or actual.status is not EvidenceState.KNOWN:
        return _result(
            rule.field,
            None,
            reason=ReasonCode.CONFLICTING_ATTRIBUTE
            if actual and actual.status is EvidenceState.CONFLICTING
            else None,
        )
    expected = rule.value
    if expected is None:
        if requested is None or requested.status is not EvidenceState.KNOWN:
            return _result(
                rule.field,
                None,
                reason=ReasonCode.CONFLICTING_ATTRIBUTE
                if requested and requested.status is EvidenceState.CONFLICTING
                else None,
            )
        if requested.unit != rule.unit:
            return _result(rule.field, None)
        expected = requested.value
    if actual.unit != rule.unit:
        return _result(rule.field, None, expected, actual.value)
    value = actual.value
    match rule.operator:
        case Operator.EQ:
            if isinstance(expected, tuple) and isinstance(value, tuple):
                left = sorted(expected) if rotation else expected
                right = sorted(value) if rotation else value
                passed = len(left) == len(right) and all(
                    abs(a - b) <= rule.tolerance
                    if isinstance(a, Decimal) and isinstance(b, Decimal)
                    else a == b
                    for a, b in zip(left, right)
                )
            elif isinstance(expected, Decimal) and isinstance(value, Decimal):
                passed = abs(expected - value) <= rule.tolerance
            else:
                passed = expected == value
        case Operator.MIN | Operator.MAX:
            if not isinstance(expected, Decimal) or not isinstance(value, Decimal):
                return _result(rule.field, None, expected, value)
            passed = (
                value >= expected - rule.tolerance
                if rule.operator is Operator.MIN
                else value <= expected + rule.tolerance
            )
        case Operator.RANGE:
            if not isinstance(expected, tuple) or not isinstance(value, Decimal):
                return _result(rule.field, None, expected, value)
            if not isinstance(expected[0], Decimal) or not isinstance(
                expected[1], Decimal
            ):
                return _result(rule.field, None, expected, value)
            passed = (
                expected[0] - rule.tolerance <= value <= expected[1] + rule.tolerance
            )
        case Operator.SET:
            if not isinstance(expected, tuple):
                return _result(rule.field, None, expected, value)
            passed = value in expected
    return _result(rule.field, passed, expected, value)


def evaluate_constraints(
    item: InputItem, candidate: Candidate, policy: Policy, *, now: datetime
) -> tuple[ConstraintResult, ...]:
    if now.tzinfo is None:
        raise ContractError("now", "timezone required")
    results: list[ConstraintResult] = []
    if (
        item.purpose is not policy.purpose
        or item.category_id not in policy.category_ids
    ):
        return (_result("policy", None, reason=ReasonCode.UNSUPPORTED_CATEGORY),)
    results.append(
        _result(
            "category_id",
            None
            if not candidate.category_id
            else candidate.category_id == item.category_id,
            item.category_id,
            candidate.category_id,
        )
    )
    results.append(
        _result(
            "item_kind",
            None
            if not item.item_kind or not candidate.item_kind
            else item.item_kind == candidate.item_kind == policy.item_kind,
            item.item_kind,
            candidate.item_kind,
        )
    )
    requested, actual = dict(item.attributes), dict(candidate.attributes)
    for rule in policy.rules:
        results.append(
            _attribute(
                rule,
                requested.get(rule.field),
                actual.get(rule.field),
                policy.allow_rotation,
            )
        )
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    results.append(
        _result(
            "contract_until",
            None
            if candidate.contract_until is None
            else candidate.contract_until >= today,
            reason=ReasonCode.EXPIRED_CONTRACT
            if candidate.contract_until and candidate.contract_until < today
            else None,
        )
    )
    results.append(_result("latest_contract", candidate.latest_contract))
    checked = dict(candidate.group_checked_at)
    for group in policy.required_groups:
        stamp = checked.get(group)
        fresh = (
            stamp is not None
            and stamp.tzinfo is not None
            and timedelta(0)
            <= now - stamp
            <= timedelta(hours=float(policy.freshness_hours))
        )
        results.append(
            _result(
                "freshness:" + group,
                True if fresh else None,
                reason=None if fresh else ReasonCode.STALE_EVIDENCE,
            )
        )
    certificates = dict(candidate.certificates)
    for certificate in policy.required_certificates:
        expiry = certificates.get(certificate)
        results.append(
            _result(
                "certificate:" + certificate,
                None if expiry is None else expiry >= today,
            )
        )
    if item.excluded_seller_ids:
        results.append(
            _result(
                "excluded_seller",
                None
                if candidate.seller_id is None
                else candidate.seller_id not in item.excluded_seller_ids,
                reason=ReasonCode.EXCLUDED_SELLER
                if candidate.seller_id in item.excluded_seller_ids
                else None,
            )
        )
    match item.purpose:
        case Purpose.EXACT_IDENTITY:
            id_match = (
                item.goods_id is not None
                and candidate.goods_id is not None
                and item.goods_id_verified
                and candidate.goods_id_verified
            )
            manufacturer_match = all(
                (
                    item.manufacturer_id,
                    candidate.manufacturer_id,
                    item.model,
                    candidate.model,
                    item.options is not None,
                    candidate.options is not None,
                )
            )
            identity = None
            if id_match:
                identity = item.goods_id == candidate.goods_id
            elif manufacturer_match:
                identity = (item.manufacturer_id, item.model, item.options) == (
                    candidate.manufacturer_id,
                    candidate.model,
                    candidate.options,
                )
            results.append(_result("identity", identity))
            for field in ("model", "options", "manufacturer_id"):
                left, right = getattr(item, field), getattr(candidate, field)
                if left is not None and right is not None:
                    results.append(_result(field, left == right, left, right))
            for name, evidence in requested.items():
                if name not in {r.field for r in policy.rules}:
                    results.append(
                        _attribute(
                            AttributeRule(
                                name,
                                Operator.EQ,
                                evidence.unit or "",
                                None,
                                Decimal(0),
                                True,
                            ),
                            evidence,
                            actual.get(name),
                            False,
                        )
                    )
        case Purpose.SAME_PRICE_COMPETITOR:
            results.append(
                _result(
                    "different_seller",
                    None
                    if not item.seller_id or not candidate.seller_id
                    else item.seller_id != candidate.seller_id,
                )
            )
            a, b = item.price, candidate.price
            fields = ("kind", "currency", "tax_basis", "unit", "pack_quantity")
            known = (
                all(
                    getattr(a, k) is not None and getattr(b, k) is not None
                    for k in fields
                )
                and a.amount is not None
                and b.amount is not None
            )
            equal = (
                all(getattr(a, k) == getattr(b, k) for k in fields)
                and a.amount == b.amount
            )
            results.append(
                _result(
                    "price_basis",
                    equal if known else None,
                    reason=None if known else ReasonCode.UNKNOWN_PRICE_BASIS,
                )
            )
        case Purpose.FUNCTIONAL_SUBSTITUTE:
            if item.price.amount is not None:
                a, b = item.price, candidate.price
                fields = ("kind", "currency", "tax_basis", "unit", "pack_quantity")
                comparable = b.amount is not None and all(
                    getattr(a, k) is not None and getattr(a, k) == getattr(b, k)
                    for k in fields
                )
                results.append(
                    _result(
                        "budget",
                        b.amount <= a.amount
                        if comparable and b.amount is not None and a.amount is not None
                        else None,
                    )
                )
    return tuple(results)
