"""Strict attribute normalization; unsupported syntax remains explicit evidence."""

import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from core.matching.contracts import AttributeEvidence, EvidenceState

_MATERIALS = (
    ("스테인리스", r"스테인(?:리스|레스)(?:강판|강|스틸)?|스텐(?:레스)?|\b(?:sus|sts)(?:\s*\d{3}L?)?(?=\b)|stainless\s*steel"),
    ("알루미늄", r"알루미늄|알미늄|알류미늄|\bal(?:uminium|uminum)?(?=\d|\b)"),
    ("접합강화유리", r"접합\s*강화\s*유리"),
    ("강화유리", r"강화\s*유리"), ("유리", r"유리"),
    ("갈바륨", r"갈바륨|갈바늄"), ("아연도금강", r"아연도금(?:강판|강철)?|전기아연도금강판|\begi\b"),
    ("갈바", r"갈바"), ("목재", r"목재|나무|원목|적삼목|낙엽송|삼나무|방부목|미송|합판|집성목|레드파인|홍송"),
    ("아크릴", r"아크릴|\bpmma\b"), ("PC", r"폴리카보네이트|\bpc\b"),
    ("HDPE", r"고밀도폴리에틸렌|\bhdpe\b"), ("PVC", r"\bpvc\b"), ("ABS", r"\babs\b"),
    ("포맥스", r"포맥스"), ("폼보드", r"폼보드"), ("플라스틱", r"플라스틱"),
    ("황동", r"황동|\bbrass\b"), ("청동", r"청동|브론즈|\bbronze\b"),
    ("구리", r"동판|구리|\bcopper\b"), ("화강석", r"화강석"),
    ("철", r"철판|스틸|\bsteel\b|강판|강재|강철|철"),
)


@dataclass(frozen=True, slots=True)
class MaterialProfile:
    raw: str
    families: frozenset[str]
    details: frozenset[str]
    grades: frozenset[str]
    recognized: bool
    terms: frozenset[str]


@lru_cache(maxsize=2048)
def material_profile(raw: str) -> MaterialProfile:
    families, details, terms = set(), set(), set()
    recognized = False
    for component in re.split(r"[/,+;]", raw.casefold()):
        component = component.strip()
        if not component:
            continue
        occupied, replacements = set(), []
        for family, pattern in _MATERIALS:
            for found in re.finditer(pattern, component, re.IGNORECASE):
                span = set(range(*found.span()))
                if not occupied.intersection(span):
                    recognized = True
                    occupied.update(span)
                    families.add(family)
                    terms.add(found.group())
                    replacement = found.group() if family == "목재" and found.group() not in ("나무", "원목") else family
                    if family == "스테인리스":
                        replacement += "".join(re.findall(r"\d{3}L?", found.group(), re.IGNORECASE))
                    replacements.append((found.start(), found.end(), replacement))
        canonical = component
        for start, end, family in sorted(replacements, reverse=True):
            canonical = canonical[:start] + family + canonical[end:]
        canonical = re.sub(r"[\s_-]+", "", canonical)
        details.add(canonical)
        if not replacements:
            families.add(canonical)
    grades = frozenset(re.sub(r"[\s-]", "", found.group()).upper() for found in re.finditer(
        r"(?<!\d)(?:\d{3}L?|A?\d{4}(?:[- ]?[HT]\d+)?)(?!\d)", raw, re.IGNORECASE))
    return MaterialProfile(raw, frozenset(families), frozenset(details), grades, recognized, frozenset(terms))


def material_rank(left: str, right: str) -> int:
    ours, candidate = material_profile(left), material_profile(right)
    if not ours.families.intersection(candidate.families):
        return 0
    if ours.grades and candidate.grades and ours.grades != candidate.grades:
        return 0
    if ours.details == candidate.details:
        return 3
    return 2 if ours.families == candidate.families else 1


def normalize_dimensions(raw: str, source_field: str = "spec") -> AttributeEvidence:
    if not raw.strip():
        return AttributeEvidence(None, None, source_field, raw, EvidenceState.MISSING)
    number = r"(\d+(?:\.\d+)?)"
    token = number + r"\s*(mm|cm)?"
    pattern = token + r"\s*[x×*]\s*" + token + r"(?:\s*[x×*]\s*" + token + r")?"
    matches = list(re.finditer(pattern, raw, re.IGNORECASE))
    if "$" in raw or len(matches) != 1:
        return AttributeEvidence(None, None, source_field, raw, EvidenceState.AMBIGUOUS)
    match = matches[0]
    if (
        match.end() < len(raw)
        and raw[match.end()].isascii()
        and raw[match.end()].isalpha()
    ):
        return AttributeEvidence(None, None, source_field, raw, EvidenceState.AMBIGUOUS)
    groups = match.groups()
    axes = [(groups[i], groups[i + 1]) for i in range(0, 6, 2) if groups[i] is not None]
    units = {unit.lower() for _, unit in axes if unit}
    if not units:
        return AttributeEvidence(None, None, source_field, raw, EvidenceState.AMBIGUOUS)
    if len(units) > 1:
        return AttributeEvidence(
            None, None, source_field, raw, EvidenceState.CONFLICTING
        )
    unit = next(iter(units))
    values = tuple(Decimal(value) * (10 if unit == "cm" else 1) for value, _ in axes)
    if any(value <= 0 for value in values):
        return AttributeEvidence(
            None, None, source_field, raw, EvidenceState.CONFLICTING
        )
    return AttributeEvidence(
        values, "mm", source_field, raw, EvidenceState.KNOWN, source_span=match.span()
    )


def normalize_material(raw: str, source_field: str = "material") -> AttributeEvidence:
    value = raw.strip().upper().replace(" ", "")
    if not value:
        return AttributeEvidence(None, None, source_field, raw, EvidenceState.MISSING)
    if any(separator in value for separator in ("/", "+", ",", "$")):
        return AttributeEvidence(None, None, source_field, raw, EvidenceState.AMBIGUOUS)
    return AttributeEvidence(value, "text", source_field, raw, EvidenceState.KNOWN)


def normalize_company_hint(raw: str) -> str:
    """A search hint only: never use this value as seller/manufacturer identity."""
    return (
        re.sub(r"\s+", "", raw).replace("(주)", "").replace("주식회사", "").casefold()
    )
