"""Verified publication locator and lifetime-pinned read-only snapshot access."""
from contextlib import contextmanager, closing, ExitStack
from dataclasses import dataclass
from hashlib import file_digest
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Literal, Mapping
from pydantic import BaseModel, ConfigDict, Field
from core.catalog_sources import get_profile, JsonValue
from src.updater.locks import file_lock, maintenance_active
from src.updater.store import Store, StoreError


class ReaderLocator(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    data_root: str
    control: Literal['control/catalog.sqlite']
    profile: str
    pin: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    manifest_schema: Literal['g2b.catalog-manifest/1']
    artifact_kind: Literal['updater_snapshot']
    environment: Literal['fixture', 'production']
    source_id: str
    scope: str
    profile_hash: str
    entity_kind: Literal['listing', 'goods']
    source_schema_version: str
    source_normalizer_version: str
    catalog_generation: int = Field(ge=1)
    publication_id: str
    source_refresh_run_id: str
    publication_kind: Literal['refresh', 'maintenance']
    published_at: str
    observed_from: str
    observed_to: str
    snapshot_path: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_schema_version: Literal[1]
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    publication_status: Literal['published']
    storage_integrity: Literal['verified']
    collection_validation: Literal['reconciled_observation']
    coverage_complete: bool
    unresolved_count: int = Field(ge=0)
    source_consistency: Literal['observation_window']
    source_snapshot_verified: Literal[False]
    information_groups: dict[str, dict[str, str | bool | None]]
    reader_locator: ReaderLocator
    request_budgets: dict[str, int]


def catalog_manifest(store: Store, *, profile_hash: str | None = None) -> Manifest:
    with store.control() as conn:
        row = conn.execute('SELECT e.manifest_json FROM publications p JOIN publication_events e ON e.run_id=p.run_id WHERE p.profile_hash=?',
                           (profile_hash or get_profile('shopping-all').profile_hash,)).fetchone()
        if row is None:
            raise StoreError('no_publication')
        return Manifest.model_validate_json(row[0])


@dataclass(frozen=True, slots=True)
class PublishedSnapshot:
    manifest: Manifest
    connection: sqlite3.Connection

    def current(self) -> Iterator[Mapping[str, JsonValue]]:
        for row in self.connection.execute('SELECT * FROM items_current ORDER BY listing_key'):
            item = json.loads(row['content_json'])
            item.update({key: row[key] for key in ('first_seen_at', 'last_checked_at', 'data_changed_at', 'price_changed_at', 'presence_status', 'observation_run_id')})
            item.update(item_version_id=row['current_version_id'], information_groups=json.loads(row['group_json']),
                        catalog_generation=self.manifest.catalog_generation, profile_hash=self.manifest.profile_hash,
                        published_at=self.manifest.published_at, snapshot_id=self.manifest.snapshot_id,
                        source_id=self.manifest.source_id, scope=self.manifest.scope,
                        freshness=dict(last_checked_at=row['last_checked_at'], presence_status=row['presence_status']))
            yield item

    def history(self, listing_key: str, *, price_only: bool = False) -> Iterator[Mapping[str, JsonValue]]:
        table = 'price_history' if price_only else 'item_versions'
        for row in self.connection.execute(f'SELECT * FROM {table} WHERE listing_key=? ORDER BY observed_at,rowid', (listing_key,)):
            yield dict(row)

    def changes(self, listing_key: str) -> Iterator[Mapping[str, JsonValue]]:
        for row in self.connection.execute('SELECT * FROM item_changes WHERE listing_key=? ORDER BY observed_at,field_path', (listing_key,)):
            yield dict(row)


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise StoreError('unsafe_path')
    current = root
    for component in path.parts:
        current = current / component
        if current.is_symlink():
            raise StoreError('symlink_path')
    return current


@contextmanager
def open_published_snapshot(manifest: Manifest | str | Path, *, root: Path | None = None, require_active: bool = False) -> Iterator[PublishedSnapshot]:
    """The configured root is trusted, never a root supplied only by the manifest."""
    if not isinstance(manifest, Manifest):
        manifest = Manifest.model_validate_json(Path(manifest).read_text())
    allowed = (root or Path(os.environ.get('NARA_DATA_DIR', 'storage/updater'))).resolve()
    if Path(manifest.reader_locator.data_root) != allowed:
        raise StoreError('untrusted_data_root')
    if manifest.reader_locator.profile != manifest.profile_hash:
        raise StoreError('profile_mismatch')
    snapshot = _safe_path(allowed, manifest.snapshot_path)
    if snapshot.parent != allowed / 'snapshots' or snapshot.suffix != '.sqlite':
        raise StoreError('unsafe_snapshot_path')
    expected_pin = f'locks/pin-{manifest.publication_id}.lock'
    if manifest.reader_locator.pin != expected_pin:
        raise StoreError('invalid_pin')
    pin = _safe_path(allowed, expected_pin)
    with ExitStack() as lifetime:
        with file_lock(_safe_path(allowed, 'locks/maintenance.lock'), shared=True):
            if require_active and maintenance_active(allowed):
                raise StoreError('maintenance_active')
            control = _safe_path(allowed, manifest.reader_locator.control)
            with closing(sqlite3.connect(f'{control.as_uri()}?mode=ro', uri=True)) as conn:
                row = conn.execute('SELECT e.manifest_json FROM publications p JOIN publication_events e ON e.run_id=p.run_id WHERE p.profile_hash=?', (manifest.profile_hash,)).fetchone()
                if row is None or Manifest.model_validate_json(row[0]) != manifest:
                    raise StoreError('stale_manifest')
            lifetime.enter_context(file_lock(pin, shared=True))
            with snapshot.open('rb') as handle:
                if file_digest(handle, 'sha256').hexdigest() != manifest.snapshot_sha256:
                    raise StoreError('snapshot_hash_mismatch')
            connection = lifetime.enter_context(closing(sqlite3.connect(f'{snapshot.as_uri()}?mode=ro&immutable=1', uri=True)))
            connection.row_factory = sqlite3.Row
        yield PublishedSnapshot(manifest, connection)
