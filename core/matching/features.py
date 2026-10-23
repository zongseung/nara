"""Train-only text vocabulary and explicit pair comparisons; identifiers never tokenize."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from math import log, sqrt
from typing import Final

from pydantic import BaseModel, ConfigDict
from sklearn.feature_extraction.text import TfidfVectorizer

from core.matching.contracts import Candidate, EvidenceState, InputItem

FEATURE_VERSION: Final = "pair-features/1"
FEATURE_NAMES: Final = (
    "text_similarity",
    "category_equal",
    "model_equal",
    "model_conflict",
    "identity_equal",
    "kind_equal",
    "missing",
    "conflicts",
    "numeric_distance",
    "unit_conflict",
    "price_relative_difference",
)


@dataclass(frozen=True, slots=True)
class Pair:
    request: InputItem
    candidate: Candidate


def text(value: InputItem | Candidate) -> str:
    """Exclude names of suppliers, raw IDs and model codes from free-text features."""
    content = f"{value.name_raw} {value.spec_raw}".lower()
    for identifier in (
        value.goods_id,
        value.seller_id,
        value.manufacturer_id,
        value.model,
    ):
        if identifier:
            content = content.replace(identifier.lower(), " ")
    return content


class Vocabulary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    terms: tuple[str, ...]
    idf: tuple[float, ...]
    version: str = FEATURE_VERSION

    @classmethod
    def fit_train(cls, pairs: tuple[Pair, ...]) -> "Vocabulary":
        vectorizer = TfidfVectorizer(
            analyzer="char", ngram_range=(2, 4), max_features=4000, sublinear_tf=True
        )
        vectorizer.fit([text(v) for p in pairs for v in (p.request, p.candidate)])
        return cls(
            terms=tuple(vectorizer.get_feature_names_out()),
            idf=tuple(float(v) for v in vectorizer.idf_),
        )

    def similarity(self, pair: Pair) -> float:
        def vector(value: InputItem | Candidate) -> list[float]:
            content = " ".join(text(value).split())
            counts = Counter(
                content[i : i + n]
                for n in (2, 3, 4)
                for i in range(max(0, len(content) - n + 1))
            )
            values = [
                (1 + log(counts[t])) * weight if counts[t] else 0.0
                for t, weight in zip(self.terms, self.idf, strict=True)
            ]
            norm = sqrt(sum(v * v for v in values)) or 1
            return [v / norm for v in values]

        return sum(
            a * b
            for a, b in zip(vector(pair.request), vector(pair.candidate), strict=True)
        )


def features(pair: Pair, vocabulary: Vocabulary) -> tuple[float, ...]:
    a, b = pair.request, pair.candidate
    left, right = dict(a.attributes), dict(b.attributes)
    missing = conflicts = units = 0
    distances: list[float] = []
    for field in sorted(left.keys() | right.keys()):
        x, y = left.get(field), right.get(field)
        if (
            x is None
            or y is None
            or x.status is not EvidenceState.KNOWN
            or y.status is not EvidenceState.KNOWN
        ):
            missing += 1
            conflicts += int(
                any(v and v.status is EvidenceState.CONFLICTING for v in (x, y))
            )
            continue
        if x.unit != y.unit:
            units += 1
            continue
        match x.value, y.value:
            case Decimal() as xv, Decimal() as yv:
                distances.append(float(abs(xv - yv) / max(abs(xv), Decimal(1))))
            case _:
                conflicts += int(x.value != y.value)
    price_delta = 0.0
    if a.price.amount is not None and b.price.amount is not None:
        comparable = all(
            getattr(a.price, k) is not None
            and getattr(a.price, k) == getattr(b.price, k)
            for k in ("kind", "currency", "unit", "tax_basis", "pack_quantity")
        )
        if comparable:
            price_delta = float(abs(a.price.amount - b.price.amount) / a.price.amount)
        else:
            missing += 1
    return (
        vocabulary.similarity(pair),
        float(a.category_id == b.category_id),
        float(bool(a.model and a.model == b.model)),
        float(bool(a.model and b.model and a.model != b.model)),
        float(
            bool(
                a.goods_id_verified and b.goods_id_verified and a.goods_id == b.goods_id
            )
        ),
        float(bool(a.item_kind and a.item_kind == b.item_kind)),
        float(missing),
        float(conflicts),
        min(sum(distances), 1e6),
        float(units),
        min(price_delta, 1e6),
    )
