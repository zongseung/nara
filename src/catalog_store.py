"""Separate diagnostic SQLite storage: append-only observations and atomic pages."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from core.catalog_partitions import Partition
from src.catalog_planner import CollectionPlan
from core.catalog_sources import SourceError, SourceProfile, entity_key
from src.g2b_client import SearchPage

from core.catalog_json import json_text


def digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA = """
CREATE TABLE source_profiles(profile_hash TEXT PRIMARY KEY, profile_json TEXT NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, profile_hash TEXT REFERENCES source_profiles,
 scope TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL,
 validation TEXT NOT NULL DEFAULT 'incomplete', expected INTEGER NOT NULL, total_size INTEGER NOT NULL,
 end_count INTEGER, end_total_size INTEGER, plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL,
 max_pages INTEGER, error TEXT, report_json TEXT);
CREATE TABLE partitions(run_id TEXT REFERENCES runs, partition_id INTEGER, lower_price TEXT,
 upper_price TEXT, expected INTEGER NOT NULL, PRIMARY KEY(run_id,partition_id));
CREATE TABLE pages(run_id TEXT, partition_id INTEGER, page_no INTEGER, request_hash TEXT,
 response_hash TEXT, fingerprint TEXT, received INTEGER, total_size INTEGER, row_total INTEGER,
 attempts INTEGER, observed_at TEXT, raw_response TEXT,
 PRIMARY KEY(run_id,partition_id,page_no), FOREIGN KEY(run_id,partition_id) REFERENCES partitions);
CREATE TABLE observations(observation_id INTEGER PRIMARY KEY, run_id TEXT, partition_id INTEGER,
 page_no INTEGER, row_no INTEGER, raw_json TEXT NOT NULL, raw_hash TEXT NOT NULL,
 observed_at TEXT, entity_key TEXT, source_id TEXT, entity_kind TEXT,
 UNIQUE(run_id,partition_id,page_no,row_no), FOREIGN KEY(run_id,partition_id,page_no) REFERENCES pages);
CREATE TABLE run_entities(run_id TEXT REFERENCES runs, entity_key TEXT, observation_id INTEGER REFERENCES observations,
 goods_id TEXT, price TEXT, name TEXT, company TEXT, PRIMARY KEY(run_id,entity_key));
CREATE TABLE quarantine(observation_id INTEGER REFERENCES observations, reason TEXT NOT NULL);
CREATE TABLE published_runs(scope TEXT PRIMARY KEY, run_id TEXT REFERENCES runs);
"""


class CatalogStore:
    """Single writer; connection lifetime is limited to the owning context."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise SourceError("store_not_open")
        return self._db

    def __enter__(self) -> Self:
        self._db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row[0] for row in tables}
        required = {
            "source_profiles",
            "runs",
            "partitions",
            "pages",
            "observations",
            "run_entities",
            "quarantine",
            "published_runs",
        }
        if (
            (version != 2 and tables)
            or version not in (0, 2)
            or (version == 2 and not required <= names)
        ):
            self.db.close()
            raise SourceError("incompatible_database", str(self.path))
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        if not tables:
            self.db.executescript(_SCHEMA + "\nPRAGMA user_version=2;")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.db.close()

    def create_run(
        self, profile: SourceProfile, plan: CollectionPlan, max_pages: int | None = None
    ) -> str:
        profile.require_collection_ready()
        run = uuid4().hex
        encoded = json_text(asdict(plan))
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO source_profiles VALUES(?,?)",
                (profile.profile_hash, json_text(asdict(profile))),
            )
            self.db.execute(
                "INSERT INTO runs(run_id,profile_hash,scope,started_at,state,expected,total_size,plan_json,plan_hash,max_pages) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    profile.profile_hash,
                    profile.scope,
                    now(),
                    "running",
                    plan.expected,
                    plan.total_size,
                    encoded,
                    digest(encoded),
                    max_pages,
                ),
            )
            self.db.executemany(
                "INSERT INTO partitions VALUES(?,?,?,?,?)",
                [
                    (
                        run,
                        i,
                        None if p.lower is None else str(p.lower),
                        None if p.upper is None else str(p.upper),
                        p.count,
                    )
                    for i, p in enumerate(plan.partitions)
                ],
            )
        return run

    def run(self, run: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run,)).fetchone()
        if row is None:
            raise SourceError("run_not_found", run)
        return row

    def profile(self, run: str) -> SourceProfile:
        row = self.db.execute(
            "SELECT profile_json FROM source_profiles WHERE profile_hash=?",
            (self.run(run)["profile_hash"],),
        ).fetchone()
        data = json.loads(row[0])
        data["name_aliases"] = tuple(data["name_aliases"])
        return SourceProfile(**data)

    def plan(self, run: str) -> CollectionPlan:
        record = self.run(run)
        if digest(record["plan_json"]) != record["plan_hash"]:
            raise SourceError("incompatible_resume", "plan_hash")
        data = json.loads(record["plan_json"], parse_float=Decimal)
        return CollectionPlan(
            tuple(
                Partition(
                    None if p["lower"] is None else Decimal(str(p["lower"])),
                    None if p["upper"] is None else Decimal(str(p["upper"])),
                    p["count"],
                )
                for p in data["partitions"]
            ),
            data["expected"],
            data["total_size"],
            None if data["quantum"] is None else Decimal(str(data["quantum"])),
            tuple(data["unresolved"]),
            data["version"],
        )

    def set_state(self, run: str, state: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET state=?,error=?,ended_at=? WHERE run_id=?",
                (state, error, now(), run),
            )

    def save_page(
        self, run: str, partition: int, number: int, page: SearchPage
    ) -> None:
        record, profile = self.run(run), self.profile(run)
        if (
            record["validation"] == "reconciled_observation"
            or page.profile.profile_hash != record["profile_hash"]
        ):
            raise SourceError("immutable_run_or_profile_mismatch")
        if len(page.rows) > 100:
            raise SourceError("page_writer_limit", "100")
        raw_response = page.raw_text or json_text(page.raw)
        encoded = [json_text(row.raw) for row in page.rows]
        stamp = page.observed_at.isoformat()
        part = self.plan(run).partitions[partition]
        with self.db:
            self.db.execute(
                "INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    partition,
                    number,
                    digest(
                        json_text(
                            {
                                "profile": profile.profile_hash,
                                "partition": asdict(part),
                                "page": number,
                            }
                        )
                    ),
                    digest(raw_response),
                    digest(json_text(sorted(digest(r) for r in encoded))),
                    len(encoded),
                    page.total_size,
                    page.row_total,
                    page.attempts,
                    stamp,
                    raw_response,
                ),
            )
            for i, (row, raw) in enumerate(zip(page.rows, encoded)):
                reason = None
                try:
                    key = entity_key(profile, row.raw)
                except SourceError as exc:
                    key, reason = None, exc.code
                cursor = self.db.execute(
                    "INSERT INTO observations(run_id,partition_id,page_no,row_no,raw_json,raw_hash,observed_at,entity_key,source_id,entity_kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        run,
                        partition,
                        number,
                        i,
                        raw,
                        digest(raw),
                        stamp,
                        key,
                        profile.source_id,
                        profile.entity_kind,
                    ),
                )
                oid = cursor.lastrowid
                goods_id = row.raw.get("itemIdnfNo")
                if goods_id is not None and not isinstance(goods_id, str):
                    reason, goods_id = "invalid_goods_reference", None
                names = [row.raw.get(alias) for alias in profile.name_aliases]
                if (
                    all(isinstance(name, str) and name.strip() for name in names)
                    and len(set(names)) > 1
                ):
                    reason = "conflicting_name_aliases"
                if key:
                    previous = self.db.execute(
                        "SELECT o.raw_hash FROM run_entities e JOIN observations o USING(observation_id) WHERE e.run_id=? AND e.entity_key=?",
                        (run, key),
                    ).fetchone()
                    if previous:
                        reason = (
                            "conflicting_observation"
                            if previous[0] != digest(raw)
                            else "duplicate_identity"
                        )
                    else:
                        self.db.execute(
                            "INSERT INTO run_entities VALUES(?,?,?,?,?,?,?)",
                            (
                                run,
                                key,
                                oid,
                                goods_id,
                                None
                                if row.contract_price_decimal is None
                                else str(row.contract_price_decimal),
                                str(
                                    row.raw.get("itemIdnfNm")
                                    or row.raw.get("itemIndfNmView")
                                    or ""
                                ),
                                str(row.raw.get("ctentUntyGrpNm") or ""),
                            ),
                        )
                if reason:
                    self.db.execute("INSERT INTO quarantine VALUES(?,?)", (oid, reason))
