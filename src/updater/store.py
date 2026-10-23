"""Control database and fenced single-writer lifecycle."""
from contextlib import contextmanager, closing, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Literal
from uuid import uuid4
from core.catalog_sources import SourceProfile
from src.updater.locks import file_lock


class StoreError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: str
    owner: str
    fence: int
    previous_run_id: str | None
    profile: SourceProfile
    environment: Literal['fixture','production'] = 'production'
    slot_key: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        yield conn


CONTROL_SCHEMA = '''
PRAGMA journal_mode=DELETE;
PRAGMA user_version=1;
CREATE TABLE IF NOT EXISTS profiles(profile_hash TEXT PRIMARY KEY, scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL REFERENCES profiles,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, cancelled INTEGER NOT NULL DEFAULT 0,
 environment TEXT NOT NULL, validation_json TEXT, previous_run_id TEXT);
CREATE TABLE IF NOT EXISTS writer_leases(profile_hash TEXT PRIMARY KEY REFERENCES profiles,
 owner TEXT, fence INTEGER NOT NULL, heartbeat TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_events(run_id TEXT PRIMARY KEY REFERENCES runs,
 profile_hash TEXT NOT NULL, generation INTEGER NOT NULL, manifest_json TEXT NOT NULL,
 UNIQUE(profile_hash,generation));
CREATE TABLE IF NOT EXISTS publications(profile_hash TEXT PRIMARY KEY REFERENCES profiles,
 run_id TEXT NOT NULL REFERENCES publication_events, generation INTEGER NOT NULL);
'''
STAGE_SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA user_version=1;
CREATE TABLE IF NOT EXISTS partitions(partition_id TEXT PRIMARY KEY, query_json TEXT NOT NULL, total INTEGER);
CREATE TABLE IF NOT EXISTS pages(partition_id TEXT NOT NULL REFERENCES partitions, page INTEGER NOT NULL,
 fingerprint TEXT NOT NULL UNIQUE, observed_at TEXT NOT NULL, raw_json TEXT NOT NULL,
 PRIMARY KEY(partition_id,page));
CREATE TABLE IF NOT EXISTS observations(listing_key TEXT PRIMARY KEY, page INTEGER NOT NULL,
 partition_id TEXT NOT NULL, raw_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 FOREIGN KEY(partition_id,page) REFERENCES pages(partition_id,page));
CREATE TABLE IF NOT EXISTS collection_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantine(id INTEGER PRIMARY KEY, reason TEXT NOT NULL, raw_json TEXT);
'''
SNAPSHOT_SCHEMA = '''
PRAGMA user_version=1;
CREATE TABLE snapshot_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE observation_evidence(evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
 listing_key TEXT NOT NULL, observed_at TEXT NOT NULL, profile_hash TEXT NOT NULL,
 raw_json TEXT NOT NULL, raw_sha256 TEXT NOT NULL);
CREATE TABLE item_versions(version_id TEXT PRIMARY KEY, listing_key TEXT NOT NULL,
 supersedes_version_id TEXT REFERENCES item_versions, run_id TEXT NOT NULL,
 observed_at TEXT NOT NULL, schema_version TEXT NOT NULL, normalizer_version TEXT NOT NULL,
 content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 evidence_id TEXT NOT NULL REFERENCES observation_evidence, UNIQUE(run_id,listing_key));
CREATE TABLE items_current(listing_key TEXT PRIMARY KEY, content_json TEXT NOT NULL,
 current_version_id TEXT NOT NULL REFERENCES item_versions,
 first_seen_at TEXT NOT NULL, last_checked_at TEXT NOT NULL, data_changed_at TEXT,
 price_changed_at TEXT, group_json TEXT NOT NULL, presence_status TEXT NOT NULL,
 observation_run_id TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES observation_evidence);
CREATE TABLE item_changes(run_id TEXT NOT NULL, listing_key TEXT NOT NULL, field_path TEXT NOT NULL,
 old_json TEXT, new_json TEXT, old_version_id TEXT NOT NULL REFERENCES item_versions,
 new_version_id TEXT NOT NULL REFERENCES item_versions, observed_at TEXT NOT NULL,
 UNIQUE(run_id,listing_key,field_path));
CREATE TABLE price_history(event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, listing_key TEXT NOT NULL,
 event_kind TEXT NOT NULL, old_amount TEXT, new_amount TEXT NOT NULL, old_unit TEXT, new_unit TEXT,
 old_currency TEXT, new_currency TEXT, observed_at TEXT NOT NULL,
 version_id TEXT NOT NULL REFERENCES item_versions, evidence_id TEXT NOT NULL REFERENCES observation_evidence,
 UNIQUE(run_id,listing_key,event_kind));
CREATE TABLE record_events(run_id TEXT NOT NULL, listing_key TEXT NOT NULL, event_kind TEXT NOT NULL,
 observed_at TEXT NOT NULL, UNIQUE(run_id,listing_key,event_kind));
'''


@dataclass(frozen=True, slots=True)
class Store:
    root: Path | None = None

    def __post_init__(self) -> None:
        root = (self.root or Path(os.environ.get('NARA_DATA_DIR', 'storage/updater'))).resolve()
        object.__setattr__(self, 'root', root)
        for name in ('control', 'stage', 'snapshots', 'locks'):
            (root / name).mkdir(parents=True, exist_ok=True)
        with self.control() as conn:
            conn.executescript(CONTROL_SCHEMA)
            if 'previous_run_id' not in {r[1] for r in conn.execute('PRAGMA table_info(runs)')}:
                conn.execute('ALTER TABLE runs ADD COLUMN previous_run_id TEXT')

    def path(self, relative: str) -> Path:
        assert self.root is not None
        return self.root / relative

    def control(self) -> AbstractContextManager[sqlite3.Connection]:
        return database(self.path('control/catalog.sqlite'))

    def stage(self, lease: Lease) -> AbstractContextManager[sqlite3.Connection]:
        return database(self.path(f'stage/{lease.run_id}.sqlite'))

    def assert_owner(self, lease: Lease) -> None:
        with self.control() as conn:
            self.check_owner(conn, lease)

    def check_owner(self, conn: sqlite3.Connection, lease: Lease) -> None:
        row = conn.execute('SELECT owner,fence FROM writer_leases WHERE profile_hash=?',
                           (lease.profile.profile_hash,)).fetchone()
        if row is None or (row['owner'], row['fence']) != (lease.owner, lease.fence):
            raise StoreError('stale_owner')
        run = conn.execute('SELECT cancelled FROM runs WHERE run_id=?', (lease.run_id,)).fetchone()
        if run is None or run['cancelled']:
            raise StoreError('cancelled')

    @contextmanager
    def writer(self, profile: SourceProfile, *, environment: Literal['fixture','production'] = 'production', resume_run_id: str | None = None) -> Iterator[Lease]:
        profile.require_collection_ready()
        try:
            with file_lock(self.path(f'locks/writer-{profile.profile_hash}.lock'), blocking=False):
                with self.control() as conn, conn:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute('INSERT OR IGNORE INTO profiles VALUES (?,?)', (profile.profile_hash, profile.scope))
                    previous = conn.execute('SELECT run_id FROM publications WHERE profile_hash=?',
                                            (profile.profile_hash,)).fetchone()
                    row = conn.execute('SELECT fence FROM writer_leases WHERE profile_hash=?',
                                       (profile.profile_hash,)).fetchone()
                    parent_id = None if previous is None else previous[0]
                    if resume_run_id is not None:
                        prior = conn.execute('SELECT * FROM runs WHERE run_id=?',(resume_run_id,)).fetchone()
                        if prior is None or prior['profile_hash'] != profile.profile_hash or prior['environment'] != environment or prior['status'] == 'published' or prior['previous_run_id'] != parent_id:
                            raise StoreError('incompatible_resume')
                        if datetime.now(timezone.utc)-datetime.fromisoformat(prior['started_at']) > timedelta(hours=12):
                            raise StoreError('expired_resume')
                    lease = Lease(resume_run_id or uuid4().hex, uuid4().hex, 1 if row is None else row[0] + 1,
                                  None if previous is None else previous[0], profile, environment)
                    conn.execute('INSERT INTO writer_leases VALUES (?,?,?,?) ON CONFLICT(profile_hash) DO UPDATE SET owner=excluded.owner,fence=excluded.fence,heartbeat=excluded.heartbeat',
                                 (profile.profile_hash, lease.owner, lease.fence, utc_now()))
                    if resume_run_id is None:
                        conn.execute('INSERT INTO runs(run_id,profile_hash,status,started_at,environment,previous_run_id) VALUES (?,?,?,?,?,?)',
                                     (lease.run_id,profile.profile_hash,'collecting',utc_now(),environment,parent_id))
                    else:
                        conn.execute("UPDATE runs SET status='collecting',finished_at=NULL,cancelled=0 WHERE run_id=?",(lease.run_id,))
                with self.stage(lease) as conn:
                    conn.executescript(STAGE_SCHEMA)
                try:
                    yield lease
                finally:
                    with self.control() as conn, conn:
                        conn.execute('UPDATE writer_leases SET owner=NULL WHERE profile_hash=? AND owner=? AND fence=?',
                                     (profile.profile_hash, lease.owner, lease.fence))
                        conn.execute("UPDATE runs SET status='failed',finished_at=? WHERE run_id=? AND status IN ('collecting','validated')", (utc_now(), lease.run_id))
        except BlockingIOError as exc:
            raise StoreError('writer_busy') from exc

    def publication(self, run_id: str) -> str | None:
        with self.control() as conn:
            row = conn.execute('SELECT manifest_json FROM publication_events WHERE run_id=?', (run_id,)).fetchone()
            return None if row is None else row[0]
