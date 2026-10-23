"""Durable business slots and bounded Prefect dispatch recovery."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import DeploymentFilter, DeploymentFilterId
from core.catalog_sources import get_profile
from src.updater.locks import maintenance_active, file_lock
from src.updater.store import Store, StoreError, utc_now

SCHEDULE_REVISION = 'daily-kst-v1'
DEPLOYMENT_NAME = 'refresh-shop-prices/g2b-price-refresh'
SCHEDULE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS scheduled_slots(slot_key TEXT PRIMARY KEY,profile_hash TEXT NOT NULL,
 schedule_revision TEXT NOT NULL,scheduled_slot_utc TEXT NOT NULL,status TEXT NOT NULL,
 business_run_id TEXT,reason TEXT,UNIQUE(profile_hash,schedule_revision,scheduled_slot_utc));
CREATE TABLE IF NOT EXISTS dispatch_outbox(attempt_key TEXT PRIMARY KEY,slot_key TEXT NOT NULL REFERENCES scheduled_slots,
 attempt INTEGER NOT NULL,state TEXT NOT NULL,prefect_run_id TEXT,available_at TEXT NOT NULL,
 UNIQUE(slot_key,attempt));
'''


def initialize(store: Store) -> None:
    with store.control() as conn:
        conn.executescript(SCHEDULE_SCHEMA)


def require_available(store: Store) -> None:
    assert store.root is not None
    if maintenance_active(store.path('')):
        raise StoreError('maintenance_active')


def latest_due(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo('Asia/Seoul'))
    due = local.replace(hour=2, minute=0, second=0, microsecond=0)
    if due > local:
        due -= timedelta(days=1)
    return due.astimezone(timezone.utc)


def ensure_slot(store: Store, due: datetime, *, manual: bool = False) -> str:
    require_available(store)
    initialize(store)
    profile = get_profile('shopping-all').profile_hash
    revision = 'manual-' + uuid4().hex if manual else SCHEDULE_REVISION
    key = sha256(f'{profile}:{revision}:{due.isoformat()}'.encode()).hexdigest()
    with store.control() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO scheduled_slots VALUES (?,?,?,?,?,?,?)',
                     (key, profile, revision, due.isoformat(), 'pending', None, None))
        conn.execute('INSERT OR IGNORE INTO dispatch_outbox VALUES (?,?,?,?,?,?)',
                     (f'{key}:0', key, 0, 'pending', None, utc_now()))
    return key


def claim_slot(store: Store, slot_key: str, run_id: str) -> bool:
    require_available(store)
    with store.control() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM scheduled_slots WHERE slot_key=?', (slot_key,)).fetchone()
        if row is None:
            raise StoreError('unknown_slot')
        obsolete = row['schedule_revision'] == SCHEDULE_REVISION and datetime.fromisoformat(row['scheduled_slot_utc']) < latest_due(datetime.now(timezone.utc))
        if obsolete:
            conn.execute("UPDATE scheduled_slots SET status='skipped_obsolete' WHERE slot_key=? AND status='pending'", (slot_key,))
            return False
        if row['business_run_id'] == run_id and row['status'] in ('running','failed'):
            conn.execute("UPDATE scheduled_slots SET status='running' WHERE slot_key=?",(slot_key,))
            return True
        if row['status'] != 'pending':
            return False
        conn.execute("UPDATE scheduled_slots SET status='running',business_run_id=? WHERE slot_key=?", (run_id, slot_key))
        return True


def _submit_pending(store: Store, slot_key: str, *, parameters: dict[str,str] | None = None) -> str | None:
    require_available(store)
    with get_client(sync_client=True) as client:
        deployment = client.read_deployment_by_name(DEPLOYMENT_NAME)
        with store.control() as conn:
            row = conn.execute('SELECT o.*,s.status FROM dispatch_outbox o JOIN scheduled_slots s USING(slot_key) WHERE slot_key=? ORDER BY attempt DESC LIMIT 1', (slot_key,)).fetchone()
        if row['status'] != 'pending':
            return None
        if datetime.fromisoformat(row['available_at']) > datetime.now(timezone.utc):
            return None
        if row['prefect_run_id']:
            prior = client.read_flow_run(UUID(row['prefect_run_id']))
            if prior.state is None or not prior.state.is_final():
                return str(prior.id)
            if not prior.state.is_cancelled() or row['attempt'] >= 3:
                with store.control() as conn, conn:
                    conn.execute("UPDATE scheduled_slots SET status='held',reason=? WHERE slot_key=?", ('dispatch_attempts_exhausted', slot_key))
                return None
            with store.control() as conn, conn:
                conn.execute("UPDATE dispatch_outbox SET state='terminal' WHERE attempt_key=?", (row['attempt_key'],))
                attempt = row['attempt'] + 1
                due = datetime.now(timezone.utc) + timedelta(seconds=(30,60,120)[attempt-1])
                conn.execute('INSERT OR IGNORE INTO dispatch_outbox VALUES (?,?,?,?,?,?)', (f'{slot_key}:{attempt}',slot_key,attempt,'pending',None,due.isoformat()))
            return None
        active = client.read_flow_runs(deployment_filter=DeploymentFilter(id=DeploymentFilterId(any_=[deployment.id])),limit=200)
        if any(r.idempotency_key != row['attempt_key'] and r.state and not r.state.is_final() and (r.expected_start_time is None or r.expected_start_time <= datetime.now(timezone.utc)) for r in active):
            return None
        run = client.create_flow_run_from_deployment(deployment.id, parameters=dict(parameters or {},slot_key=slot_key),idempotency_key=row['attempt_key'])
        with store.control() as conn, conn:
            conn.execute("UPDATE dispatch_outbox SET state='submitted',prefect_run_id=? WHERE attempt_key=?", (str(run.id),row['attempt_key']))
        return str(run.id)


def submit_pending(store: Store, slot_key: str, *, parameters: dict[str,str] | None = None) -> str | None:
    with file_lock(store.path('locks/maintenance.lock'),shared=True):
        return _submit_pending(store,slot_key,parameters=parameters)


def tick(store: Store) -> str | None:
    if maintenance_active(store.path('')) or os.environ.get('NARA_DISPATCH_ENABLED') != 'true':
        return None
    return submit_pending(store, ensure_slot(store, latest_due(datetime.now(timezone.utc))))


def main() -> None:
    store = Store()
    initialize(store)
    while True:
        tick(store)
        time.sleep(30)


if __name__ == '__main__':
    main()
