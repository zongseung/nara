"""Candidate history construction and the single control-pointer publication commit."""
from decimal import Decimal
from core.catalog_json import decimal_text
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256, file_digest
import json
import os
import sqlite3
import shutil
from uuid import uuid4
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.updater.reader import Manifest
from core.catalog_sources import parse_source_row
from src.g2b_client import SearchPage
from src.updater.source import observations_from_page
from src.updater.store import Lease, Store, StoreError, SNAPSHOT_SCHEMA, database, utc_now


def _digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def _apply(conn: sqlite3.Connection, lease: Lease, row: sqlite3.Row) -> None:
    raw = json.loads(row['raw_json'], parse_float=Decimal)
    observed = row['observed_at']
    page = SearchPage((parse_source_row(raw),), raw, 1, 1, lease.profile, datetime.fromisoformat(observed))
    item = observations_from_page(page)[0]
    old = conn.execute('SELECT * FROM items_current WHERE listing_key=?', (item.listing_key,)).fetchone()
    previous = {} if old is None else json.loads(old['content_json'])
    content = {key: value for key, value in asdict(item).items()
               if key not in ('profile', 'observed_at', 'raw', 'field_states', 'groups', 'presence_status', 'source_modified_at')}
    content['raw_names'] = dict(previous.get('raw_names', {})) | dict(item.raw_names)
    if not item.raw_names and previous:
        content['name_status'] = previous['name_status']
    content['source_modified_at'] = None if item.source_modified_at is None else item.source_modified_at.isoformat()
    content['contract_price_decimal'] = decimal_text(item.contract_price_decimal)
    if item.pack_quantity is not None:
        content['pack_quantity'] = decimal_text(item.pack_quantity)
    for key, value in previous.items():
        if content.get(key) is None or (key == 'raw_names' and not content[key]):
            content[key] = value
    groups = {} if old is None else json.loads(old['group_json'])
    for group in item.groups:
        previous_group = groups.get(group.name, {})
        groups[group.name] = dict(supported=group.supported, status=group.status,
                                 checked_at=group.checked_at.isoformat() if group.checked_at else previous_group.get('checked_at'),
                                 evidence_id=f'{lease.run_id}:{item.listing_key}' if group.checked_at else previous_group.get('evidence_id'))
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True)
    evidence = f'{lease.run_id}:{item.listing_key}'
    conn.execute('INSERT INTO observation_evidence VALUES (?,?,?,?,?,?,?)',
                 (evidence, lease.run_id, item.listing_key, observed, lease.profile.profile_hash, row['raw_json'], _digest(row['raw_json'])))
    changed = old is not None and content != previous
    version = uuid4().hex if old is None or changed else old['current_version_id']
    if old is None or changed:
        conn.execute('INSERT INTO item_versions VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (version, item.listing_key, None if old is None else old['current_version_id'], lease.run_id,
                      observed, lease.profile.source_schema_version, lease.profile.source_normalizer_version, encoded, _digest(encoded), evidence))
    if changed and old is not None:
        for field in sorted(content.keys() | previous.keys()):
            if content.get(field) != previous.get(field):
                conn.execute('INSERT INTO item_changes VALUES (?,?,?,?,?,?,?,?)',
                             (lease.run_id, item.listing_key, field, json.dumps(previous.get(field)), json.dumps(content.get(field)), old['current_version_id'], version, observed))
    price_changed = old is not None and previous['contract_price_decimal'] != content['contract_price_decimal']
    terms_changed = old is not None and any(previous.get(k) != content.get(k) for k in ('unit', 'currency'))
    kinds = (['initial'] if old is None else []) + (['price_changed'] if price_changed else []) + (['terms_changed'] if terms_changed else [])
    for kind in kinds:
        conn.execute('INSERT INTO price_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (uuid4().hex, lease.run_id, item.listing_key, kind, previous.get('contract_price_decimal'), content['contract_price_decimal'], previous.get('unit'), content['unit'], previous.get('currency'), content['currency'], observed, version, evidence))
    if old is not None and old['presence_status'] == 'not_observed':
        conn.execute('INSERT INTO record_events VALUES (?,?,?,?)', (lease.run_id, item.listing_key, 'reappeared', observed))
    conn.execute('''INSERT INTO items_current VALUES (?,?,?,?,?,?,?,?,?,?,?)
 ON CONFLICT(listing_key) DO UPDATE SET content_json=excluded.content_json,current_version_id=excluded.current_version_id,
 last_checked_at=excluded.last_checked_at,data_changed_at=excluded.data_changed_at,price_changed_at=excluded.price_changed_at,
 group_json=excluded.group_json,presence_status=excluded.presence_status,observation_run_id=excluded.observation_run_id,evidence_id=excluded.evidence_id''',
                 (item.listing_key, encoded, version, observed if old is None else old['first_seen_at'], observed,
                  observed if changed else None if old is None else old['data_changed_at'],
                  observed if price_changed else None if old is None else old['price_changed_at'], json.dumps(groups), 'observed', lease.run_id, evidence))


def publish(store: Store, lease: Lease) -> 'Manifest':
    """Idempotence is the durable event, even after a later run replaces the pointer."""
    from src.updater.reader import Manifest, ReaderLocator
    committed = store.publication(lease.run_id)
    if committed is not None:
        return Manifest.model_validate_json(committed)
    store.assert_owner(lease)
    with store.control() as conn:
        run = conn.execute('SELECT * FROM runs WHERE run_id=?', (lease.run_id,)).fetchone()
        if run['status'] != 'validated':
            raise StoreError('run_not_validated')
        validation = json.loads(run['validation_json'])
    estimate = 2 * 1024**3
    if lease.previous_run_id is not None:
        parent_path = store.path(f'snapshots/{lease.previous_run_id}.sqlite')
        estimate = max(estimate, int(parent_path.stat().st_size * 2.4))
    if shutil.disk_usage(store.path('')).free < estimate:
        raise StoreError('insufficient_disk_space')
    candidate = store.path(f'snapshots/.{lease.run_id}.{uuid4().hex}.candidate')
    final = store.path(f'snapshots/{lease.run_id}.sqlite')
    with database(candidate) as conn:
        if lease.previous_run_id is not None:
            prior = store.publication(lease.previous_run_id)
            if prior is None:
                raise StoreError('missing_parent')
            previous_manifest = Manifest.model_validate_json(prior)
            if (previous_manifest.source_schema_version, previous_manifest.source_normalizer_version) != (lease.profile.source_schema_version, lease.profile.source_normalizer_version):
                raise StoreError('migration_required')
            with database(store.path(previous_manifest.snapshot_path)) as parent:
                parent.backup(conn)
        else:
            conn.executescript(SNAPSHOT_SCHEMA)
        with conn, store.stage(lease) as stage:
            conn.execute('BEGIN IMMEDIATE')
            for row in stage.execute('SELECT * FROM observations ORDER BY listing_key'):
                _apply(conn, lease, row)
            for row in conn.execute("SELECT listing_key FROM items_current WHERE observation_run_id<>? AND presence_status='observed'", (lease.run_id,)).fetchall():
                conn.execute('INSERT INTO record_events VALUES (?,?,?,?)', (lease.run_id, row[0], 'not_observed', validation['observed_to']))
            conn.execute("UPDATE items_current SET presence_status='not_observed' WHERE observation_run_id<>?", (lease.run_id,))
            conn.execute('INSERT INTO snapshot_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('run_id', lease.run_id))
        group_summary = {}
        for group_row in conn.execute('SELECT group_json FROM items_current'):
            for name, group in json.loads(group_row[0]).items():
                summary = group_summary.setdefault(name, dict(supported=group['supported'], status=group['status'], observed_from=group['checked_at'], observed_to=group['checked_at']))
                if summary['status'] != group['status']:
                    summary['status'] = 'mixed'
                if group['checked_at']:
                    summary['observed_from'] = min(summary['observed_from'] or group['checked_at'], group['checked_at'])
                    summary['observed_to'] = max(summary['observed_to'] or group['checked_at'], group['checked_at'])
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('PRAGMA journal_mode=DELETE')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
            raise StoreError('snapshot_integrity')
    with candidate.open('rb') as handle:
        os.fsync(handle.fileno())
    os.replace(candidate, final)
    with final.open('rb') as handle:
        digest = file_digest(handle, 'sha256').hexdigest()
    descriptor = os.open(final.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    with store.control() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        store.check_owner(conn, lease)
        pointer = conn.execute('SELECT run_id,generation FROM publications WHERE profile_hash=?', (lease.profile.profile_hash,)).fetchone()
        if (None if pointer is None else pointer['run_id']) != lease.previous_run_id:
            raise StoreError('publication_conflict')
        generation = 1 if pointer is None else pointer['generation'] + 1
        manifest = Manifest(manifest_schema='g2b.catalog-manifest/1', artifact_kind='updater_snapshot',
            environment=lease.environment, source_id=lease.profile.source_id, scope=lease.profile.scope,
            profile_hash=lease.profile.profile_hash, entity_kind=lease.profile.entity_kind,
            source_schema_version=lease.profile.source_schema_version, source_normalizer_version=lease.profile.source_normalizer_version,
            catalog_generation=generation, publication_id=lease.run_id, source_refresh_run_id=lease.run_id,
            publication_kind='refresh', published_at=utc_now(), observed_from=validation['observed_from'], observed_to=validation['observed_to'],
            snapshot_path=f'snapshots/{lease.run_id}.sqlite', snapshot_sha256=digest, snapshot_schema_version=1,
            snapshot_id=_digest(json.dumps([lease.profile.source_id, lease.profile.profile_hash, generation, digest], separators=(',', ':'))),
            publication_status='published', storage_integrity='verified', collection_validation='reconciled_observation',
            coverage_complete=True, unresolved_count=0, source_consistency='observation_window', source_snapshot_verified=False,
            information_groups=group_summary, reader_locator=ReaderLocator(data_root=str(store.root), control='control/catalog.sqlite', profile=lease.profile.profile_hash, pin=f'locks/pin-{lease.run_id}.lock'),
            request_budgets={k: validation[k] for k in ('network_workers','pending_limit','http_attempts','partition_replay')})
        conn.execute('INSERT INTO publication_events VALUES (?,?,?,?)', (lease.run_id, lease.profile.profile_hash, generation, manifest.model_dump_json()))
        conn.execute('INSERT INTO publications VALUES (?,?,?) ON CONFLICT(profile_hash) DO UPDATE SET run_id=excluded.run_id,generation=excluded.generation', (lease.profile.profile_hash, lease.run_id, generation))
        conn.execute("UPDATE runs SET status='published',finished_at=? WHERE run_id=?", (manifest.published_at, lease.run_id))
        if lease.slot_key is not None:
            changed = conn.execute("UPDATE scheduled_slots SET status='published',reason=NULL WHERE slot_key=? AND business_run_id=? AND status='running'", (lease.slot_key,lease.run_id)).rowcount
            if changed != 1:
                raise StoreError('slot_publication_conflict')
    return manifest
