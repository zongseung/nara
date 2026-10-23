"""Retained SQLite, CLI, XLSX and index evidence for exact decimal publication."""
import json
from decimal import Decimal, localcontext
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from openpyxl import load_workbook
from core.catalog_json import json_text
from src.matching.catalog import open_catalog
from src.matching.index import build_index, open_index
from test.updater_fixtures import FixtureSource, row
from src.updater.collector import refresh, CollectionPolicy
from src.updater.maintenance import backup, restore, prune_files
from src.updater.reader import catalog_manifest, open_published_snapshot
from src.updater.store import Store, StoreError


def main():
    directory = Path('.omo/evidence/decimal-fidelity-review-fix') / ('surface-' + uuid4().hex[:8])
    directory.mkdir()
    store = Store(directory / 'data')
    values = ['100', '100.00', '100.00000000000000000000000001', '100.00000000000000000000000002', '100']
    with localcontext() as context:
        context.prec = 6
        for tick, price in enumerate(values):
            raw = row(price=Decimal(price))
            raw['itemIdnfNm'] = raw['itemIndfNmView'] = '=1+1'
            source_json = json_text({'rsltList': [raw]})
            (directory / f'source-{tick}.json').write_text(source_json)
            parsed = json.loads(source_json, parse_float=Decimal)['rsltList']
            manifest = refresh(store, FixtureSource(parsed, tick), policy=CollectionPolicy(request_interval=0), environment='fixture')
            with open_catalog(manifest, root=store.root) as catalog:
                descriptor = build_index(catalog, directory / f'index-{tick}')
                with open_index(descriptor, catalog=catalog) as index:
                    assert str(next(index.candidates()).price.amount) == ('100' if tick < 2 else price)
    with open_published_snapshot(manifest, root=store.root) as reader:
        key = list(reader.current())[0]['listing_key']
        history = [r['new_amount'] for r in reader.history(key, price_only=True)]
        assert history == [values[0], *values[2:]]
        raw_prices = [json.loads(r[0], parse_float=Decimal)['ctrtUprc'] for r in reader.connection.execute('SELECT raw_json FROM observation_evidence ORDER BY observed_at')]
        assert raw_prices == list(map(Decimal, values))
        assert reader.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    invocations = []
    for name, args in [('current',['current']),('history',['history',key,'--price-only']),('export',['export','--out',str(directory/'catalog.xlsx')])]:
        command = [sys.executable,'-m','src.updater.cli','--data-dir',str(store.root),*args]
        result = subprocess.run(command,capture_output=True,text=True,check=True)
        (directory/f'{name}.stdout').write_text(result.stdout or 'exit 0\n')
        invocations.append(command)
    book = load_workbook(directory/'catalog.xlsx',read_only=True)
    prices = list(book['가격이력1'].values)
    column = prices[0].index('new_amount')
    assert [r[column] for r in prices[1:]] == history
    assert book['상품1']['C2'].value == '=1+1' and book['상품1']['C2'].data_type == 's'
    book.close()
    prune_files(store)
    location = backup(store,directory/'valid-backup')
    restored = Store(directory/'restored')
    restore(restored,location)
    with open_published_snapshot(catalog_manifest(restored),root=restored.root) as reader:
        assert list(reader.current())[0]['contract_price_decimal'] == '100'
    store.path(manifest.snapshot_path).unlink()
    try:
        backup(store,directory/'invalid-backup')
    except StoreError as error:
        assert error.code == 'backup_missing_active_snapshot'
    else:
        raise AssertionError('missing active snapshot accepted')
    assert not (directory/'invalid-backup'/'backup.json').exists()
    record = {'history':history,'numeric_raw_types':[type(p).__name__ for p in raw_prices], 'index_exact':True,'xlsx_exact_and_formula_safe':True,'missing_active_backup_rejected':True,'retained_tombstone_restore_readable':True,'commands':invocations}
    (directory/'checks.json').write_text(json.dumps(record,indent=2))
    Path('.omo/evidence/decimal-fidelity-review-fix/latest-surface.json').write_text(json.dumps({'directory':str(directory),**record},indent=2))
    print(directory)


if __name__ == '__main__':
    main()
