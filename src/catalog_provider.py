"""Run-pinned, read-only diagnostic candidates for human-reviewed suggestions."""

import json
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from core.catalog_sources import RawRow, SearchQuery, SourceError


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


@dataclass(frozen=True, slots=True)
class RunMetadata:
    run_id: str
    scope: str
    profile_hash: str
    observed_from: str
    observed_to: str | None
    state: str
    validation: str
    incomplete: bool
    source_snapshot_verified: bool = False
    search_semantics: str = (
        "Unicode NFKC casefold whitespace-token AND substring; not server-equivalent"
    )


@dataclass(frozen=True, slots=True)
class CandidatePage:
    rows: tuple[RawRow, ...]
    total: int
    truncated: bool
    metadata: RunMetadata | None


class CandidateProvider(Protocol):
    def search(
        self, keyword: str, lower: str = "", upper: str = "", limit: int = 500
    ) -> CandidatePage: ...

    def enrich(self, raw: RawRow, *, required: tuple[str, ...] = ()) -> RawRow: ...


class LiveProvider:
    def search(self, keyword: str, lower: str = "", upper: str = "", limit: int = 500) -> CandidatePage:
        import requests

        from src.g2b_client import fetch_search_page, get_profile
        if limit < 1:
            raise SourceError("invalid_limit")
        rows, page, total = [], 1, 0
        with requests.Session() as session:
            while len(rows) < limit:
                result = fetch_search_page(SearchQuery(page=page, keyword=keyword, start_price=lower,
                                                       end_price=upper), get_profile("shopping-all"), session=session)
                total = result.row_total or 0
                rows.extend(row.raw for row in result.rows)
                if len(result.rows) < 100 or len(rows) >= total:
                    break
                page += 1
        return CandidatePage(tuple(rows[:limit]), total, total > limit, None)

    def enrich(self, raw: RawRow, *, required: tuple[str, ...] = ()) -> RawRow:
        from src.g2b_client import enrich_product
        return enrich_product(raw, required=required)


@contextmanager
def read_db(path: str | Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        if con.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise SourceError("incompatible_database")
        yield con
    finally:
        con.close()


class SQLiteProvider:
    """Pin one run at construction; each thread opens its own read-only connection."""

    def __init__(
        self,
        db: str | Path,
        run: str = "last-reconciled",
        scope: str = "shopping-all",
        allow_incomplete: bool = False,
    ):
        self.path = Path(db)
        with read_db(db) as con:
            if run == "last-reconciled":
                selected = con.execute(
                    "SELECT run_id FROM published_runs WHERE scope=?", (scope,)
                ).fetchone()
                if selected is None:
                    raise SourceError("no_reconciled_run", scope)
                run = selected[0]
            row = con.execute(
                "SELECT * FROM runs WHERE run_id=? AND scope=?", (run, scope)
            ).fetchone()
            if row is None:
                raise SourceError("run_not_found", run)
            incomplete = row["validation"] != "reconciled_observation"
            if incomplete and not allow_incomplete:
                raise SourceError("incomplete_run", run)
            self.metadata = RunMetadata(
                run,
                scope,
                row["profile_hash"],
                row["started_at"],
                row["ended_at"],
                row["state"],
                row["validation"],
                incomplete,
            )

    def search(
        self, keyword: str, lower: str = "", upper: str = "", limit: int = 500
    ) -> CandidatePage:
        if limit < 1:
            raise SourceError("invalid_limit")
        tokens = normalized(keyword).split()
        selected, total = [], 0
        with read_db(self.path) as con:
            # Streaming keeps memory bounded even when substring search cannot use an index.
            rows = con.execute(
                "SELECT e.price,o.raw_json FROM run_entities e JOIN observations o USING(observation_id) WHERE e.run_id=? ORDER BY e.entity_key",
                (self.metadata.run_id,),
            )
            for row in rows:
                raw = json.loads(row["raw_json"], parse_float=Decimal)
                haystack = normalized(
                    " ".join(
                        str(raw.get(k) or "")
                        for k in (
                            "itemIdnfNm",
                            "itemIndfNmView",
                            "dtlsPrnm",
                            "pdctAtrbCdDtlNm",
                        )
                    )
                )
                if not all(t in haystack for t in tokens):
                    continue
                if (lower or upper) and (row["price"] is None or
                        (lower and Decimal(row["price"]) < Decimal(lower)) or
                        (upper and Decimal(row["price"]) > Decimal(upper))):
                    continue
                total += 1
                if len(selected) < limit:
                    selected.append(raw)
        return CandidatePage(tuple(selected), total, total > limit, self.metadata)

    def description(self) -> str:
        return json.dumps(asdict(self.metadata), ensure_ascii=False)

    def enrich(self, raw: RawRow, *, required: tuple[str, ...] = ()) -> RawRow:
        return raw
