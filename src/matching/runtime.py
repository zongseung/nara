"""Protected installation identity and one pinned workbook run."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from core.matching.contracts import ContractError, Purpose
from src.matching.installation import installation
from src.matching.releases import ReleaseAuthority, read_release


def run_workbook(args: argparse.Namespace) -> None:
    from src.updater.locks import file_lock
    with file_lock(Path(args.ledger).with_suffix('.operations.lock'), shared=True):
        _run_workbook(args)


def _run_workbook(args: argparse.Namespace) -> None:
    from src.format_convert import read_input_items
    from src.matching.catalog import open_catalog
    from src.matching.engine import run_match
    from src.matching.index import build_index, open_index
    from src.matching.models import load_model
    from core.matching.policy import PolicyRegistry
    from src.matching.policy import read_policy
    from src.matching.reporting import write_report
    from src.matching.reviews import read_registry
    from src.matching.store import MatchingStore

    if Path(args.out).exists():
        raise ContractError('report', 'output already exists')
    release = read_release(Path(args.release))
    purpose = Purpose(args.purpose)
    if release.manifest.purpose != purpose or release.manifest.category_id != args.category:
        raise ContractError('release', 'purpose/category mismatch')
    policy = PolicyRegistry(tuple(read_policy(path) for path in Path(args.policy_dir).glob('*.json'))).get(purpose, args.category)
    if policy is None or policy.content_hash != release.manifest.policy_hash:
        raise ContractError('policy', 'missing cell or release hash mismatch')
    items = read_input_items(args.input, purpose=purpose, category_id=args.category)
    if args.limit:
        raise ContractError('limit', 'partial workbook runs are forbidden in shadow/auto')
    if not items:
        raise ContractError('input', 'no item rows')
    config = installation() if args.mode == 'auto' or release.model_hash or release.registry_hash else None
    authority = ReleaseAuthority(config.authority_root) if config else None
    if args.mode == 'auto':
        if config is None or Path(args.source_root).resolve() != config.source_root.resolve() or Path(args.ledger).resolve() != config.ledger.resolve():
            raise ContractError('installation', 'auto requires configured source and ledger')
        requested_index = Path(args.index_dir)
        if requested_index.is_symlink() or requested_index.resolve() != config.index_dir.resolve():
            raise ContractError('index', 'auto requires configured protected index root')
        from src.matching.monitoring import require_cell_active
        require_cell_active(config, release)
        if authority is None:
            raise ContractError("authority", "protected active authority required")
        authority.require_active(release)
    scorer = load_model(config.artifact_root, release.model_hash) if config and release.model_hash else None
    registry = read_registry(config.artifact_root / (release.registry_hash + '.registry.json'), expected_hash=release.registry_hash) if config and release.registry_hash else None
    now = datetime.now(UTC)
    rows = []
    with open_catalog(Path(args.catalog_manifest), root=Path(args.source_root), production=args.mode == 'auto') as catalog:
        descriptor = Path(args.index_dir) / (catalog.manifest.snapshot_id + '.json')
        if not descriptor.exists():
            descriptor = build_index(catalog, Path(args.index_dir))
        with open_index(descriptor, catalog=catalog) as index, MatchingStore(Path(args.ledger)) as ledger:
            if args.mode == 'auto' and config is not None:
                for cached in (index.manifest.descriptor_path, index.manifest.path):
                    info = cached.stat()
                    if cached.is_symlink() or cached.resolve().parent != config.index_dir.resolve() or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                        raise ContractError('index', 'untrusted cache file ownership, permissions or path')
            for item in items:
                result = run_match(item, policy, index, release=release, now=now, scorer=scorer, registry=registry, mode='production' if args.mode == 'auto' else 'shadow', authority=authority)
                job_id = ledger.record_job(item, result, release)
                rows.append((item, result, job_id))
            write_report(Path(args.out), rows, release=release, manifest=catalog.manifest, observed_at=now, mode=args.mode, connection=ledger.connection)
    print(json.dumps({'report': args.out, 'rows': len(rows), 'mode': args.mode, 'statuses': {name: sum(result.decision.status.value == name for _, result, _ in rows) for name in sorted({r.decision.status.value for _, r, _ in rows})}}, ensure_ascii=False))
