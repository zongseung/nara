"""Explicit maintenance gate, coherent backup and immutable history retention."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
from typing import TypedDict
import shutil
from src.updater.locks import file_lock
from src.updater.reader import Manifest, catalog_manifest, open_published_snapshot
from src.updater.store import Store, StoreError, database, utc_now
from core.catalog_sources import get_profile


def enter_maintenance(store: Store) -> None:
    store.path('control/maintenance.flag').write_text(utc_now())


def resume(store: Store) -> None:
    """Operator action after source and matching restore validation."""
    manifest=catalog_manifest(store)
    with open_published_snapshot(manifest,root=store.root) as snapshot:
        if snapshot.connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or snapshot.connection.execute('PRAGMA foreign_key_check').fetchall():
            raise StoreError('resume_integrity')
    store.path('control/maintenance.flag').unlink(missing_ok=True)


@contextmanager
def maintenance_window(store: Store):
    enter_maintenance(store)
    with file_lock(store.path('locks/maintenance.lock')):
        with file_lock(store.path(f'locks/writer-{get_profile("shopping-all").profile_hash}.lock')):
            yield


class Freshness(TypedDict):
    status: str
    age_seconds: float
    observed_from: str
    observed_to: str
    source_refresh_run_id: str
    published_at: str


def freshness(manifest: Manifest, now: datetime | None = None) -> Freshness:
    age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(manifest.observed_from)).total_seconds()
    status = 'stale' if age > 72*3600 else 'warning' if age > 36*3600 else 'fresh'
    return Freshness(status=status,age_seconds=max(0,age),observed_from=manifest.observed_from,
                observed_to=manifest.observed_to,source_refresh_run_id=manifest.source_refresh_run_id,
                published_at=manifest.published_at)


def disk_preflight(store: Store) -> int:
    size = sum(path.stat().st_size for directory in ('stage','snapshots') for path in store.path(directory).glob('*.sqlite'))
    required = max(2*1024**3,int(size*3*1.2))
    if shutil.disk_usage(store.path('')).free < required:
        raise StoreError('insufficient_disk_space')
    return required


def backup(store: Store, destination: Path) -> Path:
    disk_preflight(store)
    destination.mkdir(parents=True,exist_ok=False)
    with maintenance_window(store):
        with store.control() as source, database(destination/'catalog.sqlite') as target:
            source.backup(target)
            current = source.execute('SELECT manifest_json FROM publication_events ORDER BY generation').fetchall()
            active = {r[0] for r in source.execute('SELECT run_id FROM publications')}
            if not active <= {Manifest.model_validate_json(r[0]).publication_id for r in current}:
                raise StoreError('backup_missing_active_manifest')
        files = []
        for row in current:
            manifest = Manifest.model_validate_json(row[0])
            if not store.path(manifest.snapshot_path).exists():
                if manifest.publication_id in active:
                    raise StoreError('backup_missing_active_snapshot')
                continue  # Retention tombstones are historical, never active pointers.
            target = destination/manifest.snapshot_path
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(store.path(manifest.snapshot_path),target)
            with target.open('rb') as handle:
                digest=file_digest(handle,'sha256').hexdigest()
            if digest != manifest.snapshot_sha256:
                raise StoreError('backup_hash_mismatch')
            files.append(dict(path=manifest.snapshot_path,sha256=digest))
        with (destination/'catalog.sqlite').open('rb') as handle:
            control_hash=file_digest(handle,'sha256').hexdigest()
        (destination/'backup.json').write_text(json.dumps(dict(schema=1,control_sha256=control_hash,snapshots=files),indent=2))
    return destination


def restore(store: Store, source: Path) -> None:
    """Gate remains active after restore; existing pins cause refusal, never overwrite."""
    record=json.loads((source/'backup.json').read_text())
    with (source/'catalog.sqlite').open('rb') as handle:
        if file_digest(handle,'sha256').hexdigest()!=record['control_sha256']:
            raise StoreError('backup_hash_mismatch')
    with database(source/'catalog.sqlite') as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
            raise StoreError('backup_integrity')
        events = {m.snapshot_path: m for row in conn.execute('SELECT manifest_json FROM publication_events')
                  for m in (Manifest.model_validate_json(row[0]),)}
        active = conn.execute('SELECT p.run_id,p.profile_hash,p.generation,e.manifest_json FROM publications p LEFT JOIN publication_events e ON e.run_id=p.run_id').fetchall()
        required = set()
        for row in active:
            if row[3] is None:
                raise StoreError('backup_missing_active_manifest')
            manifest = Manifest.model_validate_json(row[3])
            if (manifest.publication_id,manifest.profile_hash,manifest.catalog_generation) != tuple(row[:3]):
                raise StoreError('backup_active_manifest_mismatch')
            required.add(manifest.snapshot_path)
        listed = {item['path'] for item in record['snapshots']}
        if not required <= listed or len(listed) != len(record['snapshots']):
            raise StoreError('backup_missing_active_snapshot')
        if any(item['path'] not in events or item['sha256'] != events[item['path']].snapshot_sha256 for item in record['snapshots']):
            raise StoreError('backup_manifest_mismatch')
    for item in record['snapshots']:
        relative=Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts or len(relative.parts)!=2 or relative.parts[0]!='snapshots':
            raise StoreError('unsafe_backup_path')
        if not (source/relative).is_file() or (source/relative).is_symlink():
            raise StoreError('backup_missing_snapshot')
        with (source/relative).open('rb') as handle:
            if file_digest(handle,'sha256').hexdigest()!=item['sha256']:
                raise StoreError('backup_hash_mismatch')
    with maintenance_window(store), ExitStack() as pins:
        for path in store.path('snapshots').glob('*.sqlite'):
            try:
                pins.enter_context(file_lock(store.path(f'locks/pin-{path.stem}.lock'),blocking=False))
            except BlockingIOError as exc:
                raise StoreError('restore_pinned_snapshot') from exc
        for item in record['snapshots']:
            target=store.path(item['path'])
            temporary=target.with_suffix('.restore')
            shutil.copy2(source/item['path'],temporary)
            os.replace(temporary,target)
        with database(source/'catalog.sqlite') as original, store.control() as target:
            original.backup(target)
            with target:
                for row in target.execute('SELECT run_id,manifest_json FROM publication_events').fetchall():
                    manifest=Manifest.model_validate_json(row['manifest_json'])
                    locator=manifest.reader_locator.model_copy(update=dict(data_root=str(store.root)))
                    manifest=manifest.model_copy(update=dict(reader_locator=locator))
                    target.execute('UPDATE publication_events SET manifest_json=? WHERE run_id=?',(manifest.model_dump_json(),row['run_id']))
    catalog_manifest(store)


def prune_files(store: Store, *, now: datetime | None = None) -> dict[str,list[str]]:
    cutoff=(now or datetime.now(timezone.utc))-timedelta(days=7)
    removed=[]
    with maintenance_window(store):
        with store.control() as conn:
            protected={r[0] for r in conn.execute('SELECT run_id FROM publications')}
            protected.update(r[0] for r in conn.execute('SELECT run_id FROM publication_events ORDER BY generation DESC LIMIT 3'))
            terminal=conn.execute("SELECT run_id FROM runs WHERE finished_at<? AND status IN ('failed','published')",(cutoff.isoformat(),)).fetchall()
        for row in terminal:
            for suffix in ('','.sqlite-wal','.sqlite-shm'):
                name=f'{row[0]}.sqlite' if not suffix else f'{row[0]}{suffix}'
                store.path('stage/'+name).unlink(missing_ok=True)
            for candidate in store.path('snapshots').glob(f'.{row[0]}.*.candidate'):
                candidate.unlink()
        for path in store.path('snapshots').glob('*.sqlite'):
            if path.stem in protected:
                continue
            try:
                with file_lock(store.path(f'locks/pin-{path.stem}.lock'),blocking=False):
                    path.unlink()
                    removed.append(path.name)
            except BlockingIOError:
                continue
    return dict(removed=removed)


def prune_history(store: Store, *, now: datetime | None = None) -> Manifest:
    """Publish a maintenance generation; source observations stay fixed."""
    cutoff=((now or datetime.now(timezone.utc))-timedelta(days=365)).isoformat()
    enter_maintenance(store)
    with file_lock(store.path('locks/maintenance.lock')):
        with store.writer(get_profile('shopping-all'),environment=catalog_manifest(store).environment) as lease:
            previous=catalog_manifest(store)
            final=store.path(f'snapshots/{lease.run_id}.sqlite')
            candidate=final.with_suffix('.candidate')
            with database(store.path(previous.snapshot_path)) as original, database(candidate) as conn:
                original.backup(conn)
                with conn:
                    conn.execute('DELETE FROM item_changes WHERE observed_at<?',(cutoff,))
                    conn.execute('DELETE FROM price_history WHERE observed_at<?',(cutoff,))
                    conn.execute('DELETE FROM record_events WHERE observed_at<?',(cutoff,))
                    conn.execute('CREATE TEMP TABLE keep_versions(version_id TEXT PRIMARY KEY)')
                    conn.execute('INSERT OR IGNORE INTO keep_versions SELECT current_version_id FROM items_current')
                    conn.execute('INSERT OR IGNORE INTO keep_versions SELECT version_id FROM item_versions WHERE observed_at>=?',(cutoff,))
                    conn.execute('INSERT OR IGNORE INTO keep_versions SELECT version_id FROM item_versions v WHERE observed_at<? AND observed_at=(SELECT max(observed_at) FROM item_versions b WHERE b.listing_key=v.listing_key AND b.observed_at<?)',(cutoff,cutoff))
                    for table,column in (('item_changes','old_version_id'),('item_changes','new_version_id'),('price_history','version_id')):
                        conn.execute(f'INSERT OR IGNORE INTO keep_versions SELECT {column} FROM {table}')
                    conn.execute('UPDATE item_versions SET supersedes_version_id=NULL WHERE supersedes_version_id NOT IN (SELECT version_id FROM keep_versions)')
                    conn.execute('DELETE FROM item_versions WHERE version_id NOT IN (SELECT version_id FROM keep_versions)')
                    conn.execute("DELETE FROM observation_evidence WHERE evidence_id NOT IN (SELECT evidence_id FROM item_versions UNION SELECT evidence_id FROM items_current UNION SELECT evidence_id FROM price_history) AND evidence_id NOT IN (SELECT json_extract(g.value,'$.evidence_id') FROM items_current i,json_each(i.group_json) g WHERE json_extract(g.value,'$.evidence_id') IS NOT NULL)")
                    conn.execute('INSERT INTO snapshot_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('retention_boundary',cutoff))
                if conn.execute('PRAGMA foreign_key_check').fetchall() or conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise StoreError('maintenance_integrity')
                conn.execute('PRAGMA journal_mode=DELETE')
            with candidate.open('rb') as handle:
                os.fsync(handle.fileno())
                digest=file_digest(handle,'sha256').hexdigest()
            os.replace(candidate,final)
            directory=os.open(final.parent,os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            manifest=previous.model_copy(update=dict(publication_id=lease.run_id,publication_kind='maintenance',catalog_generation=previous.catalog_generation+1,
                snapshot_path=f'snapshots/{lease.run_id}.sqlite',snapshot_sha256=digest,published_at=utc_now(),
                snapshot_id=sha256(json.dumps([previous.source_id,previous.profile_hash,previous.catalog_generation+1,digest],separators=(',',':')).encode()).hexdigest(),
                reader_locator=previous.reader_locator.model_copy(update=dict(pin=f'locks/pin-{lease.run_id}.lock'))))
            with store.control() as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                store.check_owner(conn,lease)
                current=conn.execute('SELECT run_id FROM publications WHERE profile_hash=?',(previous.profile_hash,)).fetchone()
                if current[0]!=previous.publication_id:
                    raise StoreError('publication_conflict')
                conn.execute('INSERT INTO publication_events VALUES (?,?,?,?)',(lease.run_id,previous.profile_hash,manifest.catalog_generation,manifest.model_dump_json()))
                conn.execute('UPDATE publications SET run_id=?,generation=? WHERE profile_hash=?',(lease.run_id,manifest.catalog_generation,previous.profile_hash))
                conn.execute("UPDATE runs SET status='published',finished_at=? WHERE run_id=?",(manifest.published_at,lease.run_id))
            return manifest
