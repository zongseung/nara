"""Local append-only matching ledger; SQLite transactions serialize every write."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import TypeAdapter

from core.matching.contracts import ContractError, InputItem, dumps_contract, loads_contract
from src.matching.datasets import Label, SplitManifest, freeze_split
from src.matching.engine import EngineResult
from src.matching.releases import OperatorContext, Release
from src.matching.reviews import Approval, Review, pair_key


class MatchingStore:
    """Owns one thread-confined writer; BEGIN IMMEDIATE and unique keys cover other processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
CREATE TABLE IF NOT EXISTS model_releases (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request TEXT NOT NULL, release_id TEXT NOT NULL REFERENCES model_releases(id), snapshot_id TEXT NOT NULL, config TEXT NOT NULL, supersedes TEXT REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS decisions (job_id TEXT PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL, evidence_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_checks (job_id TEXT REFERENCES jobs(id), candidate_id TEXT, payload TEXT NOT NULL, evidence_hash TEXT NOT NULL, PRIMARY KEY(job_id,candidate_id));
CREATE TABLE IF NOT EXISTS review_events (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), candidate_id TEXT NOT NULL, payload TEXT NOT NULL, principal TEXT NOT NULL, resolution_status TEXT NOT NULL, pending_approval TEXT);
CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dataset_splits (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (test_hash TEXT PRIMARY KEY, release_hash TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consumed_test_identities (identity_hash TEXT PRIMARY KEY, test_hash TEXT NOT NULL REFERENCES evaluations(test_hash));
""")
        for table in (
            "model_releases",
            "jobs",
            "decisions",
            "candidate_checks",
            "review_events",
            "labels",
            "dataset_splits",
            "evaluations",
            "consumed_test_identities",
        ):
            for operation in ("UPDATE", "DELETE"):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only'); END"
                )
        self.connection.commit()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.connection.close()

    def record_job(
        self,
        item: InputItem,
        result: EngineResult,
        release: Release,
        *,
        supersedes: str | None = None,
    ) -> str:
        if result.evaluation_only or result.decision.environment == "evaluation":
            raise ContractError(
                "ledger", "evaluation results cannot publish business decisions"
            )
        if (
            result.release_hash != release.content_hash
            or result.decision.input_row_id != item.input_row_id
        ):
            raise ContractError("ledger", "result/request/release mismatch")
        request = dumps_contract(item)
        config = release.model_dump_json()
        source = result.retrieval.snapshot_id
        job_id = sha256(
            (request + config + source + result.retrieval.index_generation).encode()
        ).hexdigest()
        payload = TypeAdapter(EngineResult).dump_json(result).decode()
        evidence = sha256(payload.encode()).hexdigest()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.connection.execute(
                "SELECT evidence_hash FROM decisions WHERE job_id=?", (job_id,)
            ).fetchone()
            if old is not None:
                if old[0] != evidence:
                    raise ContractError(
                        "job", "same canonical inputs produced different evidence"
                    )
                return job_id
            release_id = sha256(config.encode()).hexdigest()
            self.connection.execute(
                "INSERT OR IGNORE INTO model_releases VALUES (?,?)",
                (release_id, config),
            )
            self.connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                (job_id, request, release_id, source, config, supersedes),
            )
            self.connection.execute(
                "INSERT INTO decisions VALUES (?,?,?)", (job_id, payload, evidence)
            )
            for check in result.candidate_checks:
                copied = TypeAdapter(type(check)).dump_json(check).decode()
                self.connection.execute(
                    "INSERT INTO candidate_checks VALUES (?,?,?,?)",
                    (
                        job_id,
                        check.candidate.listing_key,
                        copied,
                        sha256(copied.encode()).hexdigest(),
                    ),
                )
        return job_id

    def import_review(
        self, review: Review, operator: OperatorContext, *, now: datetime
    ) -> Approval | None:
        operator.require("reviewer")
        if (
            not review.reason
            or review.reviewed_at.tzinfo is None
            or review.expires_at.tzinfo is None
            or not review.reviewed_at <= now < review.expires_at
        ):
            raise ContractError("review", "missing reason or stale/future review")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT c.payload,c.evidence_hash,j.request,j.config FROM candidate_checks c JOIN jobs j ON j.id=c.job_id WHERE c.job_id=? AND c.candidate_id=?",
                (review.job_id, review.candidate_id),
            ).fetchone()
            if row is None or row[1] != review.evidence_hash:
                raise ContractError("review", "unknown pair or changed evidence")
            old = self.connection.execute(
                "SELECT payload,principal,pending_approval FROM review_events WHERE id=?",
                (review.review_id,),
            ).fetchone()
            if old is not None:
                if old[0] != review.model_dump_json() or old[1] != operator.principal:
                    raise ContractError("review_id", "conflicting repeated import")
                return Approval.model_validate_json(old[2]) if old[2] else None
            from src.matching.engine import CandidateCheck

            check = TypeAdapter(CandidateCheck).validate_json(row[0])
            item = loads_contract(row[2])
            if not isinstance(item, InputItem):
                raise ContractError("review", "invalid stored request")
            release = Release.model_validate_json(row[3])
            approval = (
                Approval(
                    pair_key=pair_key(item, check.candidate, release),
                    evidence_hash=review.evidence_hash,
                    approved_at=review.reviewed_at,
                    expires_at=review.expires_at,
                    reviewer=operator.principal,
                )
                if review.target
                else None
            )
            self.connection.execute(
                "INSERT INTO review_events VALUES (?,?,?,?,?,?,?)",
                (
                    review.review_id,
                    review.job_id,
                    review.candidate_id,
                    review.model_dump_json(),
                    operator.principal,
                    "human_confirmed" if review.target else "human_rejected",
                    approval.model_dump_json() if approval else None,
                ),
            )
        return approval

    def import_labels(
        self, labels: tuple[Label, ...], operator: OperatorContext
    ) -> None:
        operator.require("reviewer")
        freeze_split(labels)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for label in labels:
                if label.reviewer != operator.principal:
                    raise ContractError(
                        "label", "reviewer differs from trusted principal"
                    )
                payload = json.dumps(label.to_payload(), sort_keys=True)
                old = self.connection.execute(
                    "SELECT payload FROM labels WHERE id=?", (label.label_id,)
                ).fetchone()
                if old is not None and old[0] != payload:
                    raise ContractError("label", "immutable label changed")
                self.connection.execute(
                    "INSERT OR IGNORE INTO labels VALUES (?,?)",
                    (label.label_id, payload),
                )

    def record_split(self, split: SplitManifest) -> None:
        payload = TypeAdapter(SplitManifest).dump_json(split).decode()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO dataset_splits VALUES (?,?)",
                (split.content_hash, payload),
            )

    def claim_test(
        self, test_hash: str, release_hash: str, *, identities: tuple[str, ...] = ()
    ) -> None:
        """Claim before test execution; interrupted tests remain consumed and cannot tune/retry."""
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                legacy = self.connection.execute(
                    "SELECT 1 FROM evaluations e WHERE NOT EXISTS (SELECT 1 FROM consumed_test_identities c WHERE c.test_hash=e.test_hash) LIMIT 1"
                ).fetchone()
                if legacy is not None:
                    raise ContractError(
                        "final_test",
                        "legacy consumed study lacks identities; cannot safely reopen holdouts",
                    )
                self.connection.execute(
                    "INSERT INTO evaluations VALUES (?,?,?)",
                    (test_hash, release_hash, "consumed/2"),
                )
                self.connection.executemany(
                    "INSERT INTO consumed_test_identities VALUES (?,?)",
                    (
                        (identity, test_hash)
                        for identity in sorted(
                            set(identities) | {"partition:" + test_hash}
                        )
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ContractError(
                "final_test", "heldout identity already consumed in this study"
            ) from exc

    def backup(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as destination:
            self.connection.backup(destination)
