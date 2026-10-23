"""Hash-checked source, ledger and actual immutable artifact bundles."""

import json
import shutil
import sqlite3
from contextlib import closing
from hashlib import file_digest
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.matching.contracts import ContractError
from src.matching.installation import Installation
from src.matching.store import MatchingStore
from src.updater import maintenance
from src.updater.store import Store


class BackupInventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    schema_version: Literal[1] = Field(alias='schema')
    files: dict[str, str]


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def _roots(config: Installation) -> dict[str, Path]:
    return {'artifacts': config.artifact_root, 'authority': config.authority_root, 'policies': config.policy_dir, 'datasets': config.dataset_root}


def _health(config: Installation) -> None:
    from src.matching.installation import validate_assets
    from src.matching.models import load_model
    from src.matching.releases import Release, ReleaseAuthority, read_release
    from src.matching.reviews import read_registry
    with MatchingStore(config.ledger) as ledger:
        for row in ledger.connection.execute('SELECT payload FROM model_releases'):
            release = Release.model_validate_json(row[0])
            if release.model_hash:
                load_model(config.artifact_root, release.model_hash)
            if release.registry_hash:
                read_registry(config.artifact_root / (release.registry_hash + '.registry.json'), expected_hash=release.registry_hash)
    for artifact in config.artifact_root.glob('*/artifact.json'):
        load_model(config.artifact_root, artifact.parent.name)
    active = config.authority_root / 'active-release.json'
    if active.exists():
        release = read_release(active)
        validate_assets(config, release)
        ReleaseAuthority(config.authority_root).require_active(release)


def backup(config: Installation, destination: Path) -> None:
    if not config.ledger.is_file():
        raise ContractError('backup', 'matching ledger missing')
    destination = destination.resolve()
    if any(destination.is_relative_to(root.resolve()) for root in (*_roots(config).values(), config.source_root)):
        raise ContractError('backup', 'destination must be outside managed roots')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    maintenance.backup(Store(config.source_root), destination / 'source')
    _health(config)
    with MatchingStore(config.ledger) as ledger:
        ledger.backup(destination / 'matching.sqlite')
    for name, root in _roots(config).items():
        target = destination / name
        target.mkdir()
        if not root.exists():
            continue
        for original in root.rglob('*'):
            if original.is_symlink():
                raise ContractError('backup', 'symlink artifact forbidden')
            if not original.is_file() or original.name.endswith(('-wal', '-shm', '.lock')):
                continue
            copied = target / original.relative_to(root)
            copied.parent.mkdir(parents=True, exist_ok=True)
            if original.suffix == '.sqlite':
                with closing(sqlite3.connect(original)) as source, closing(sqlite3.connect(copied)) as dest:
                    source.backup(dest)
            else:
                shutil.copyfile(original, copied)
    files = {str(path.relative_to(destination)): _digest(path) for path in destination.rglob('*') if path.is_file()}
    (destination / 'matching-backup.json').write_text(json.dumps({'schema': 1, 'files': files}, sort_keys=True, indent=2))
    print(json.dumps({'backup': str(destination), 'files': len(files), 'maintenance': True}))


def restore(config: Installation, source: Path) -> None:
    # A failed restore must leave automatic jobs stopped, including hash failures.
    maintenance.enter_maintenance(Store(config.source_root))
    if source.is_symlink():
        raise ContractError('backup', 'symlink bundle forbidden')
    source = source.resolve()
    try:
        record = BackupInventory.model_validate_json((source / 'matching-backup.json').read_text())
    except ValidationError as exc:
        raise ContractError('backup', 'malformed bundle manifest') from exc
    files = record.files
    actual = {str(path.relative_to(source)) for path in source.rglob('*') if path.is_file() and path.name != 'matching-backup.json'}
    if set(files) != actual or 'matching.sqlite' not in files:
        raise ContractError('backup', 'missing or extra bundle files')
    allowed = {'source', *_roots(config)}
    for relative, digest in files.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or (relative != 'matching.sqlite' and path.parts[0] not in allowed):
            raise ContractError('backup', 'unsafe file path')
        target = source / path
        if any(parent.is_symlink() for parent in (target, *target.parents)) or _digest(target) != digest:
            raise ContractError('backup', 'artifact hash mismatch')
    maintenance.restore(Store(config.source_root), source / 'source')
    with closing(sqlite3.connect(source / 'matching.sqlite')) as ledger:
        if ledger.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or ledger.execute('PRAGMA foreign_key_check').fetchall():
            raise ContractError('backup', 'ledger integrity failure')
        config.ledger.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(config.ledger)) as destination:
            ledger.backup(destination)
    for name, root in _roots(config).items():
        for original in (source / name).rglob('*'):
            if original.is_file():
                target = root / original.relative_to(source / name)
                if any((root / part).is_symlink() for part in (original.relative_to(source / name), *original.relative_to(source / name).parents)):
                    raise ContractError('restore', 'destination symlink')
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                staged = target.with_name(target.name + '.restoring')
                with staged.open('xb') as stream, original.open('rb') as incoming:
                    shutil.copyfileobj(incoming, stream)
                staged.chmod(0o600)
                staged.replace(target)
    active_pointer = config.authority_root / 'active-release.json'
    if active_pointer.exists() and not (source / 'authority' / 'active-release.json').exists():
        active_pointer.unlink()
    _health(config)
    _rebuild(config)
    print(json.dumps({'restored': str(source), 'maintenance': True, 'resume_required': True}))


def _rebuild(config: Installation) -> None:
    from src.matching.catalog import open_catalog
    from src.matching.index import build_index
    from src.updater.reader import catalog_manifest
    manifest = catalog_manifest(Store(config.source_root))
    with open_catalog(manifest, root=config.source_root) as catalog:
        build_index(catalog, config.index_dir)


def resume(config: Installation) -> None:
    _health(config)
    _rebuild(config)
    maintenance.resume(Store(config.source_root))
    print(json.dumps({'maintenance': False, 'schedules': 'remain inactive'}))
