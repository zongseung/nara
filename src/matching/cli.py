"""Offline matching commands; optional CPU dependencies load after argument parsing."""

import argparse
import json
import sqlite3
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('input')
    run.add_argument('--mode', choices=('shadow', 'auto'), default='shadow')
    for name in ('purpose', 'category', 'catalog-manifest', 'source-root', 'policy-dir', 'release', 'ledger', 'index-dir', 'out'):
        run.add_argument('--' + name, required=True)
    run.set_defaults(limit=0)
    index = commands.add_parser('index')
    for name in ('catalog-manifest', 'source-root', 'out'):
        index.add_argument('--' + name, required=True)
    for name in ('labels-import', 'review-import'):
        commands.add_parser(name).add_argument('input', type=Path)
    export = commands.add_parser('review-export')
    export.add_argument('--ledger', required=True, type=Path)
    export.add_argument('--out', required=True, type=Path)
    train = commands.add_parser('train')
    for name in ('labels', 'pairs', 'split', 'out'):
        train.add_argument('--' + name, required=True, type=Path)
    evaluate = commands.add_parser('evaluate')
    for name in ('labels', 'pairs', 'split', 'release', 'catalog-manifest', 'out'):
        evaluate.add_argument('--' + name, required=True, type=Path)
    evaluate.add_argument('--cases', type=Path, help='operator-frozen holdout/absence evidence manifest')
    status = commands.add_parser('status')
    status.add_argument('--ledger', required=True, type=Path)
    rollback = commands.add_parser('rollback')
    rollback.add_argument('--release', type=Path)
    rollback.add_argument('--purpose', required=True)
    rollback.add_argument('--category', required=True)
    audit = commands.add_parser('audit')
    audit.add_argument('--review-id', required=True)
    for name in ('backup', 'restore'):
        commands.add_parser(name).add_argument('path', type=Path)
    commands.add_parser('resume')
    return result


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    from core.catalog_sources import SourceError
    from src.matching.catalog import CatalogError
    from core.matching.contracts import ContractError
    from src.updater.store import StoreError
    try:
        match args.command:
            case 'run':
                from src.matching.runtime import run_workbook
                run_workbook(args)
            case 'index':
                from src.matching.catalog import open_catalog
                from src.matching.index import build_index
                with open_catalog(args.catalog_manifest, root=Path(args.source_root)) as catalog:
                    index = build_index(catalog, Path(args.out))
                    print(json.dumps({'index': str(index.descriptor_path), 'rows': index.row_count, 'environment': index.environment}))
            case 'review-export':
                from src.matching.operator_cli import export_reviews
                export_reviews(args.ledger, args.out)
            case 'status':
                from src.matching.monitoring import status
                print(json.dumps(status(args.ledger), ensure_ascii=False))
            case _:
                from src.matching.operator_cli import execute
                execute(args)
    except (ContractError, CatalogError, SourceError, StoreError, ValueError, OSError, sqlite3.Error) as exc:
        ap.exit(2, f'error: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
