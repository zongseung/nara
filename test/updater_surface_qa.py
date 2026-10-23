"""Run: .venv/bin/python -m test.updater_surface_qa; retains fixture SQLite/XLSX artifacts."""
import json
from pathlib import Path
import subprocess
import sys
import sqlite3
from contextlib import closing
from uuid import uuid4
from openpyxl import load_workbook
from test.updater_fixtures import FixtureSource, row
from src.updater.collector import CollectionPolicy, refresh
from src.updater.reader import open_published_snapshot
from src.updater.store import Store


def main():
    evidence = Path('.omo/evidence/updater-publication')
    artifact = evidence / ('surface-' + uuid4().hex[:12])
    artifact.mkdir(parents=True)
    store = Store(artifact / 'data')
    policy = CollectionPolicy(request_interval=0)
    manifests = []
    for tick, price in enumerate(('100','100.0','120','100')):
        product = row(price=price)
        product['itemIdnfNm'] = product['itemIndfNmView'] = '=1+1'
        manifests.append(refresh(store, FixtureSource([product],tick),policy=policy,environment='fixture'))
    manifest = manifests[-1]
    checks = {}
    with open_published_snapshot(manifest, root=store.root) as reader:
        checks['prices'] = [r['new_amount'] for r in reader.history('shopping:listing:ctrtItemMngNo:A',price_only=True)]
        checks['version_count'] = reader.connection.execute('SELECT count(*) FROM item_versions').fetchone()[0]
        checks['integrity'] = reader.connection.execute('PRAGMA integrity_check').fetchone()[0]
        checks['foreign_keys'] = reader.connection.execute('PRAGMA foreign_key_check').fetchall()
    first_path = store.path(manifests[0].snapshot_path)
    with closing(sqlite3.connect(f'{first_path.as_uri()}?mode=ro',uri=True)) as first:
        checks['first_price_change_null'] = first.execute('SELECT price_changed_at FROM items_current').fetchone()[0] is None
    commands = [('status',['status','--json']),('current',['current']),
                ('history',['history','shopping:listing:ctrtItemMngNo:A','--price-only']),
                ('manifest',['catalog-manifest','--out',str(artifact / 'manifest.json')]),
                ('export',['export','--out',str(artifact / 'catalog.xlsx')])]
    invocations = []
    for name, args in commands:
        invocation = [sys.executable,'-m','src.updater.cli','--data-dir',str(store.root),*args]
        result = subprocess.run(invocation,capture_output=True,text=True)
        (artifact / f'{name}.stdout').write_text(result.stdout or f'exit_code={result.returncode}\n')
        (artifact / f'{name}.stderr').write_text(result.stderr or 'stderr empty\n')
        invocations.append(dict(command=invocation,exit_code=result.returncode))
        if result.returncode:
            raise RuntimeError(result.stderr)
    book = load_workbook(artifact / 'catalog.xlsx',read_only=True,data_only=False)
    checks['excel_formula_is_text'] = book.active['C2'].value == '=1+1' and book.active['C2'].data_type == 's'
    checks['excel_kst'] = '[Asia/Seoul]' in book.active['M2'].value
    book.close()
    assert checks['prices'] == ['100','120','100']
    assert checks['version_count'] == 3 and checks['first_price_change_null']
    assert checks['integrity'] == 'ok' and checks['foreign_keys'] == []
    assert checks['excel_formula_is_text'] and checks['excel_kst']
    (artifact / 'checks.json').write_text(json.dumps(checks,indent=2))
    (artifact / 'invocations.json').write_text(json.dumps(invocations,indent=2))
    (evidence / 'latest-surface.json').write_text(json.dumps(dict(artifact=str(artifact),checks=checks),indent=2))
    print(json.dumps(dict(artifact=str(artifact),checks=checks),indent=2))


if __name__ == '__main__':
    main()
