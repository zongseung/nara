"""Exact JSON numeric serialization shared by catalog producers."""
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal

type JsonDocument = (
    None
    | bool
    | int
    | float
    | Decimal
    | str
    | Mapping[str, JsonDocument]
    | Sequence[JsonDocument]
)


def json_text(value: JsonDocument) -> str:
    """Serialize Decimal numbers without rounding or changing their JSON type."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not JSON")
        return str(value)
    if isinstance(value, Mapping):
        return (
            "{"
            + ",".join(
                json.dumps(k, ensure_ascii=False) + ":" + json_text(v)
                for k, v in sorted(value.items())
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(json_text(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def decimal_text(value: Decimal) -> str:
    """Canonical finite decimal text independent of the active decimal context."""
    if not value.is_finite():
        raise ValueError("non-finite Decimal key")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
