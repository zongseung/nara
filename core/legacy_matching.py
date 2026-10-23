import difflib
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, NotRequired, TypedDict

from core.catalog_sources import JsonValue, RawRow, parse_source_row
from core.matching.normalization import material_profile, material_rank

ComparisonItem = TypedDict("ComparisonItem", {
    "업체": str, "식별번호": str | None, "품명": str, "세부품명": str,
    "세부품명번호": str, "이름": str, "규격": tuple[Decimal, Decimal] | None,
    "규격원문": str, "형상": str, "재질": str, "재질원문": str, "용도": str,
    "방향수": int | None, "표시면수": int | None, "지지대수": int | None, "속성": dict[str, str],
    "속성충돌": tuple[str, ...], "가격": Decimal | None, "모델": str,
    "imgSrc": str, "계약구분": str, "_raw": RawRow,
    "점수": NotRequired[float], "점수상세": NotRequired[dict[str, float]], "규격가격우선": NotRequired[bool],
    "매칭근거": NotRequired[str], "근사": NotRequired[bool], "catalog_metadata": NotRequired[dict[str, JsonValue]],
    "비교보류사유": NotRequired[str],
})


def _to_price(v):
    """단가 안전 파싱 → 정수. 이상값(NaN·문자·None)은 0."""
    try:
        n = int(Decimal(str(v)))
        return max(n, 0)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0


def _norm_company(name):
    """업체명 정규화: 법인격·공백 제거 후 소문자. exact 집합 비교용(substring 오제외 방지)."""
    s = str(name or "")
    for t in ("주식회사", "(주)", "㈜", "유한회사", "(유)", "(재)", "(사)"):
        s = s.replace(t, "")
    return "".join(s.split()).lower()
WEIGHTS = {"재질": 0.55, "용도": 0.20, "규격": 0.10, "가격": 0.15}
SPEC_TOL: Final = 0.10


def norm_material(s: str | None) -> str:
    return "/".join(sorted(material_profile(s or "").families))


def _length(raw: str) -> Decimal | None:
    found = re.fullmatch(r"\s*[ΦφØø]?\s*(\d+(?:\.\d+)?)\s*(mm|cm|m)?\s*", raw, re.IGNORECASE)
    if not found:
        return None
    value = Decimal(found[1]) * {None: 1, "mm": 1, "cm": 10, "m": 1000}[found[2].lower() if found[2] else None]
    return value if value > 0 else None


def parse_dims(s: str | None) -> tuple[Decimal, Decimal] | None:
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?\s*(?:mm|cm|m)?)\s*[×xX*]\s*(\d+(?:\.\d+)?\s*(mm|cm|m)?)", s or "", re.IGNORECASE)
    if not m:
        return None
    left = m[1] if re.search(r"[a-z]", m[1], re.IGNORECASE) else m[1] + (m[3] or "mm")
    width, height = _length(left), _length(m[2])
    return (width, height) if width is not None and height is not None else None


def parse_use(s):
    """'... 용도:도로명판걸이' → '도로명판걸이'."""
    m = re.search(r"용도\s*[:：]\s*([^,|]+)", s or "")
    return m.group(1).strip() if m else ""


@dataclass(frozen=True, slots=True)
class AttributeSet:
    values: tuple[tuple[str, str], ...]
    conflicts: tuple[str, ...]


def product_attributes(raw: RawRow) -> AttributeSet:
    definitions, values, conflicts = {}, {}, set()
    for part in str(raw.get("pdctAtrbNm") or "").split("|"):
        fields = part.split("$")
        if len(fields) >= 2:
            definitions[fields[-1]] = html.unescape(fields[-2]).strip()
    for part in str(raw.get("pdctAtrbCdDtl") or "").split("|"):
        encoded, separator, value = part.partition("$")
        codes = [code for code in definitions if encoded.startswith(code)]
        if separator and codes:
            label = definitions[max(codes, key=len)]
            decoded = html.unescape(value).strip()
            if label in values and values[label] != decoded:
                conflicts.add(label)
            values[label] = decoded
    for label, value in re.findall(r"([^:,|$]+)[:：]([^,|$]+)", str(raw.get("pdctAtrbCdDtlNm") or "")):
        values.setdefault(label.strip(), html.unescape(value).strip())
    detail = raw.get("_detail_attributes")
    if isinstance(detail, list):
        for attribute in detail:
            if not isinstance(attribute, dict):
                continue
            label = html.unescape(str(attribute.get("atrbnm") or "")).strip()
            raw_value = attribute.get("atrbvalue")
            value = html.unescape(str(raw_value if raw_value is not None else "")).strip()
            unit = str(attribute.get("atrboptnvalue") or "").strip()
            if not label or not value:
                continue
            if unit and not value.endswith(unit):
                value += unit
            same_length = label in values and _length(value) is not None and _length(values[label]) == _length(value)
            if label in values and values[label] != value and not same_length:
                conflicts.add(label)
            values.setdefault(label, value)
    return AttributeSet(tuple(values.items()), tuple(sorted(conflicts)))


def direction_count(text: str) -> int | None:
    match = re.search(r"(?<!\d)(\d+)\s*방향", text)
    if match:
        return int(match[1]) or None
    for word, number in (("단방향", 1), ("일방향", 1), ("양방향", 2), ("두방향", 2)):
        if word in text.replace(" ", ""):
            return number
    return None


def support_counts(name: str, attributes: Mapping[str, str]) -> frozenset[int]:
    prefix = r"(?:지지대|지주|기둥|다리)"
    counts: set[int] = set()
    for label, value in attributes.items():
        key = re.sub(r"\s+|\([^)]*\)", "", label)
        number = re.fullmatch(r"([0-9]+)(?:\.0+)?\s*(?:개|본|EA)?", value, re.IGNORECASE)
        if re.fullmatch(prefix + r"(?:수|수량|개수)?", key) and number:
            counts.add(int(number[1]))
        if re.fullmatch(prefix + r"(?:유무|포함여부)?", key) and value in ("없음", "무", "미포함"):
            counts.add(0)
    text = name + " " + " ".join(f"{key}:{value}" for key, value in attributes.items())
    counts.update(0 if excluded else int(value) for value, excluded in re.findall(
        prefix + r"\s*[:：]?\s*([0-9]+)\s*(?:개|본)\s*(미포함|없음)?(?![가-힣A-Za-z])", text))
    counts.update(int(value) for value in re.findall(
        r"(?<![0-9A-Za-z])([0-9]+)\s*주(?:식|형)(?![가-힣A-Za-z0-9])", text))
    if re.search(prefix + r"\s*[:：]?\s*(?:없음|미포함)(?![가-힣])", text):
        counts.add(0)
    return frozenset(counts)


def missing_product_fields(item: ComparisonItem, required: tuple[str, ...] = ()) -> tuple[str, ...]:
    evidence = dict(item["속성"], **item)
    return tuple(key for key in ("재질원문", "용도", "규격", *required)
                 if (value := evidence.get(key)) is None or value == "")


def _model(name):
    """이름 토큰 중 영문+숫자 섞인 걸 모델명 후보로."""
    for tok in re.split(r"[,\s]+", name or ""):
        if re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok):
            return tok.strip("()")
    return ""


def extract(raw: RawRow) -> ComparisonItem:
    """API 원본 row → 매칭용 dict (원본은 _raw로 보관)."""
    attrs = str(raw.get("pdctAtrbCdDtlNm") or "")
    name = html.unescape(str(raw.get("itemIdnfNm") or raw.get("itemIndfNmView") or ""))
    attributes = product_attributes(raw)
    values = dict(attributes.values)
    material = values.get("재질") or "/".join(part.strip() for part in attrs.split("$") if material_profile(part).recognized)
    if not material:
        material = "/".join(sorted(material_profile(name).terms))
    use = values.get("용도") or parse_use(attrs)
    width, height = values.get("표지크기(가로)", ""), values.get("표지크기(세로)", "")
    w, h = _length(width), _length(height)
    dims = (w, h) if w is not None and h is not None else parse_dims(name) or parse_dims(attrs)
    options = values.get("옵션/기타", "")
    faces = options + " " + name
    circle = re.search(r"[ΦφØø]\s*\d+(?:\.\d+)?\s*mm", name)
    conflicts = set(attributes.conflicts)
    use_direction, name_direction = direction_count(use), direction_count(name)
    if use_direction is not None and name_direction is not None and use_direction != name_direction:
        conflicts.add("방향수")
    if "양면" in faces and "단면" in faces:
        conflicts.add("표시면수")
    supports = support_counts(name, values)
    if len(supports) > 1:
        conflicts.add("지지대수")
    return {
        "업체": html.unescape(str(raw.get("mnftrEtpsNm") or raw.get("ctentUntyGrpNm") or "")),
        "식별번호": str(raw["itemIdnfNo"]) if raw.get("itemIdnfNo") else None,
        "품명": name.split(",")[0].strip() if name else "",   # itemIdnfNm 첫 토큰(안내판/간판)
        "세부품명": str(raw.get("dtlsPrnm") or ""),
        "세부품명번호": str(raw.get("dtlsPrnmNo") or ""),
        "이름": name,
        "규격": dims,
        "규격원문": f"{width}×{height}" if width and height else circle.group() if circle else "",
        "형상": "원형" if re.search(r"[ΦφØø]", width + name) else "직사각형" if dims else "미상",
        "재질": norm_material(material),
        "재질원문": material,
        "용도": use,
        "방향수": direction_count(use + " " + name),
        "표시면수": 2 if "양면" in faces else 1 if "단면" in faces else None,
        "지지대수": next(iter(supports)) if len(supports) == 1 else None,
        "속성": values,
        "속성충돌": tuple(sorted(conflicts)),
        "가격": parse_source_row(raw).contract_price_decimal,
        "모델": _model(name),
        "imgSrc": html.unescape(str(raw.get("imgSrc") or "")),
        "계약구분": str(raw.get("shopCtrtTyNm") or ""),
        "_raw": raw,
    }


def score(our, cand):
    """(총점, 항목별점수). our/cand 모두 extract 형식(우리 품목은 손으로 채운 dict)."""
    p = {}
    # 재질: 양쪽 다 norm_material 로 정규화 후 동일 여부 (1순위 — 순위를 지배).
    p["재질"] = material_rank(our.get("재질원문") or our.get("재질", ""), cand.get("재질원문") or cand.get("재질", "")) / 3
    # 두 변 각각의 상대차 중 큰 쪽이 SPEC_TOL 이내면 규격 점수 만점.
    ov, cv = our.get("규격"), cand.get("규격")
    if ov and cv and ov[0] and ov[1]:
        d = float(max(abs(ov[0] - cv[0]) / ov[0], abs(ov[1] - cv[1]) / ov[1]))
        p["규격"] = 1.0 if d <= SPEC_TOL else max(0.0, 1 - (d - SPEC_TOL))
    else:
        p["규격"] = 0.7
    p["용도"] = difflib.SequenceMatcher(None, our.get("용도", ""), cand.get("용도", "")).ratio() if our.get("용도") and cand.get("용도") else 0.0
    op, cp = _to_price(our.get("가격")), _to_price(cand.get("가격"))
    p["가격"] = max(0.0, 1 - abs(op - cp) / op) if op else 0.5
    return sum(WEIGHTS[k] * p[k] for k in WEIGHTS), p
