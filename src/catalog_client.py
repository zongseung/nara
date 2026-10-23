"""Diagnostic source gates and a single bounded shared transport seam."""

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from core.catalog_sources import (
    SearchQuery,
    SourceError,
    SourceProfile,
    get_profile,
)
from src.g2b_client import SearchPage, fetch_search_page, parse_search_page
from src.transport import RequestLimiter


class Source(Protocol):
    profile: SourceProfile

    def fetch(self, query: SearchQuery) -> SearchPage: ...


def diagnostic_profile(scope: str) -> SourceProfile:
    aliases = {"catalog-goods": "catalog", "digital-main": "digital"}
    profile = get_profile(aliases.get(scope, scope))
    if scope == "digital-main":
        profile = replace(profile, scope=scope, purchase_method="본010001")
    elif scope == "catalog-goods":
        profile = replace(profile, scope=scope)
    return profile


def require_ready(profile: SourceProfile) -> None:
    if profile.source_id == "catalog":
        code = (
            "requires_contract_validation"
            if os.environ.get("DATA_GO_KR_SERVICE_KEY")
            else "requires_credentials"
        )
        raise SourceError(
            code,
            "15129417: classification coverage, pagination, status and count contracts",
        )
    if profile.validation_status == "unvalidated":
        raise SourceError("requires_contract_validation", profile.scope)
    profile.require_collection_ready()


class NetworkSource:
    """Own a shared request-start limiter; transport owns the sole HTTP retry loop."""

    def __init__(self, profile: SourceProfile, rate: float = 1):
        require_ready(profile)
        if rate <= 0:
            raise SourceError("invalid_rate")
        self.profile = profile
        self.limiter = RequestLimiter(rate=rate)

    def fetch(self, query: SearchQuery) -> SearchPage:
        return fetch_search_page(
            query, self.profile, timeout=(10, 35), limiter=self.limiter
        )


class FixtureSource:
    """Explicit offline fixture adapter; never impersonates live source validation."""

    def __init__(self, path: Path, profile: SourceProfile):
        require_ready(profile)
        from decimal import Decimal

        try:
            body = json.loads(path.read_text(), parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise SourceError("invalid_fixture", "json") from exc
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            raise SourceError("invalid_fixture")
        if not all(isinstance(row, dict) for row in body["rows"]):
            raise SourceError("invalid_fixture", "rows")
        self.rows = body["rows"]
        self.profile = replace(profile, endpoint="fixture:" + str(path.resolve()))

    def fetch(self, query: SearchQuery) -> SearchPage:
        from decimal import Decimal

        rows = self.rows
        if query.start_price:
            rows = [
                r
                for r in rows
                if r.get("ctrtUprc") is not None
                and Decimal(query.start_price)
                <= Decimal(str(r["ctrtUprc"]))
                <= Decimal(query.end_price)
            ]
        if query.keyword:
            rows = [r for r in rows if query.keyword in str(r)]
        total = len(rows)
        size = min(query.page_size, self.profile.page_cap)
        selected = rows[(query.page - 1) * size : query.page * size]
        return parse_search_page(
            {"totalSize": total, "rsltList": [dict(r, totCnt=total) for r in selected]},
            self.profile,
        )
