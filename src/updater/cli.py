"""Read-only operational command line; refresh dispatch belongs to Prefect."""
import argparse
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypedDict
from core.catalog_sources import JsonValue, SourceError
from src.updater.maintenance import Freshness
from src.updater.exports import export_xlsx, export_sample
from src.updater.reader import catalog_manifest, open_published_snapshot
from src.updater.store import Store, StoreError


class StatusReport(TypedDict, total=False):
    runs: list[dict[str,JsonValue]]
    maintenance: bool
    freshness: Freshness | dict[str,str]
    information_groups: dict[str,dict[str,str|bool|None]]
    last_successful_refresh_at: str
    source_enabled: bool
    dispatch_enabled: bool
    collection_progress: dict[str, int | str]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status')
    status.add_argument('--json', action='store_true')
    manifest = commands.add_parser('catalog-manifest')
    manifest.add_argument('--out', type=Path, required=True)
    commands.add_parser('current')
    refresh = commands.add_parser('refresh')
    refresh.add_argument('--mode',choices=['full'],default='full')
    refresh.add_argument('--retry-slot')
    history = commands.add_parser('history')
    history.add_argument('listing_key')
    history.add_argument('--price-only', action='store_true')
    history.add_argument('--changes', action='store_true')
    export = commands.add_parser('export')
    export.add_argument('--out', type=Path, required=True)
    sample = commands.add_parser('sample', help='Export a bounded live test sample without publishing to the operational store.')
    sample.add_argument('--limit', type=int, default=100, metavar='1..1000')
    sample.add_argument('--out', type=Path, required=True)
    collect = commands.add_parser('collect', help='Collect live pages in separate processes without a database or Docker.')
    collect.add_argument('--keyword', default='')
    collect.add_argument('--company', default='')
    collect.add_argument('--min-price', default='')
    collect.add_argument('--max-price', default='')
    collect.add_argument('--limit', type=int, default=1000, metavar='1..10000')
    collect.add_argument('--workers', type=int, default=4, metavar='1..4')
    collect.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'collect':
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        from concurrent.futures.process import BrokenProcessPool
        from core.catalog_sources import SearchQuery
        from src.live_collection import export_live
        try:
            query=SearchQuery(keyword=args.keyword,company=args.company,
                              start_price=args.min_price,end_price=args.max_price)
            print(json.dumps(export_live(query,args.out,limit=args.limit,workers=args.workers),ensure_ascii=False))
        except (SourceError,OSError,BrokenProcessPool) as error:
            parser.exit(2,f'수집 실패: {error}\n')
        except KeyboardInterrupt:
            parser.exit(130,'Collection interrupted.\n')
        return
    if args.command == 'sample':
        from src.updater.source import ShoppingSource
        try:
            print(json.dumps(export_sample(ShoppingSource(), args.out, limit=args.limit), ensure_ascii=False))
        except (StoreError, SourceError, OSError) as error:
            parser.exit(2, f'{error}\n')
        return
    store = Store(args.data_dir)
    if args.command == 'refresh':
        if os.environ.get('NARA_SOURCE_ENABLED') != 'true':
            parser.exit(2, 'production_source_not_enabled: Set NARA_SOURCE_ENABLED=true in docker/.env '
                        + 'and recreate updater-worker before submitting a refresh.\n')
        from datetime import datetime, timezone
        from src.updater.dispatcher import ensure_slot, submit_pending
        key = args.retry_slot or ensure_slot(store,datetime.now(timezone.utc),manual=True)
        if args.retry_slot:
            from src.updater.dispatcher import require_available
            from prefect.client.orchestration import get_client
            from src.updater.dispatcher import DEPLOYMENT_NAME
            from uuid import uuid4
            from src.updater.locks import file_lock
            with file_lock(store.path('locks/maintenance.lock'),shared=True):
                require_available(store)
                with get_client(sync_client=True) as client:
                    deployment=client.read_deployment_by_name(DEPLOYMENT_NAME)
                    run=client.create_flow_run_from_deployment(deployment.id,parameters=dict(slot_key=key),idempotency_key='manual-retry-'+uuid4().hex)
                    print(json.dumps(dict(slot_key=key,prefect_run_id=str(run.id))))
        else:
            print(json.dumps(dict(slot_key=key,prefect_run_id=submit_pending(store,key))))
        return
    if args.command == 'status':
        with store.control() as conn:
            runs=[dict(row) for row in conn.execute('SELECT * FROM runs ORDER BY started_at')]
        from src.updater.maintenance import freshness
        from src.updater.locks import maintenance_active
        report: StatusReport=StatusReport(runs=runs,maintenance=maintenance_active(store.path('')),
                                         source_enabled=os.environ.get('NARA_SOURCE_ENABLED')=='true',
                                         dispatch_enabled=os.environ.get('NARA_DISPATCH_ENABLED')=='true')
        if runs:
            stage=store.path(f"stage/{runs[-1]['run_id']}.sqlite")
            if stage.is_file():
                with closing(sqlite3.connect(f'{stage.as_uri()}?mode=ro', uri=True)) as conn:
                    metadata=conn.execute("SELECT value FROM collection_metadata WHERE key='policy'").fetchone()
                    size=json.loads(metadata[0])['page_size'] if metadata else 100
                    total=conn.execute('SELECT COALESCE(sum(total),0) FROM partitions').fetchone()[0]
                    report['collection_progress']=dict(run_id=str(runs[-1]['run_id']),expected_items=total,
                        collected_items=conn.execute('SELECT count(*) FROM observations').fetchone()[0],
                        completed_pages=conn.execute('SELECT count(*) FROM pages').fetchone()[0],
                        expected_pages=conn.execute('SELECT COALESCE(sum((total+?-1)/?),0) FROM partitions',(size,size)).fetchone()[0])
        try:
            current=catalog_manifest(store)
            report['freshness']=freshness(current)
            report['information_groups']=current.information_groups
            original=store.publication(current.source_refresh_run_id)
            if original:
                report['last_successful_refresh_at']=json.loads(original)['published_at']
        except StoreError as error:
            if error.code!='no_publication':
                raise
            report['freshness']=dict(status='unpublished')
        print(json.dumps(report))
        return
    try:
        manifest = catalog_manifest(store)
    except StoreError as error:
        if error.code != 'no_publication':
            raise
        parser.exit(2, 'no_publication: No published catalog is available.\n'
                    + 'Run status --json to check collection progress.\n'
                    + 'Complete the first source refresh and publication before reading or exporting.\n'
                    + 'Docker startup does not collect data; source access is disabled by default.\n')
    if args.command == 'catalog-manifest':
        args.out.write_text(manifest.model_dump_json(indent=2))
        return
    with open_published_snapshot(manifest, root=store.root) as snapshot:
        if args.command == 'export':
            export_xlsx(snapshot, args.out)
        elif args.command == 'current':
            for item in snapshot.current():
                print(json.dumps(item, ensure_ascii=False))
        else:
            rows = snapshot.changes(args.listing_key) if args.changes else snapshot.history(args.listing_key, price_only=args.price_only)
            for row in rows:
                print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
