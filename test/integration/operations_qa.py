"""Explicit sandbox scheduler recovery and container restart/backup surface checks."""
import json
import os
from pathlib import Path
import subprocess
from test.integration.test_price_updater_stack import COMPOSE,EVIDENCE,command


def main():
    command(['stop','updater-worker'])
    script='''
from datetime import datetime,timezone
import json
from uuid import UUID
from prefect.client.orchestration import get_client
from prefect.states import Cancelled
from src.updater.store import Store
from src.updater.dispatcher import ensure_slot,submit_pending,DEPLOYMENT_NAME
store=Store()
key=ensure_slot(store,datetime.now(timezone.utc),manual=True)
with get_client(sync_client=True) as client:
    deployment=client.read_deployment_by_name(DEPLOYMENT_NAME)
    assert deployment.paused and all(not schedule.active for schedule in deployment.schedules)
    lost=client.create_flow_run_from_deployment(deployment.id,parameters=dict(slot_key=key),idempotency_key=key+':0')
    recovered=submit_pending(store,key)
    assert recovered==str(lost.id)
    ids=[recovered]
    for attempt in range(4):
        client.set_flow_run_state(UUID(ids[-1]),Cancelled(),force=True)
        submit_pending(store,key)
        if attempt<3:
            with store.control() as conn,conn:
                conn.execute("UPDATE dispatch_outbox SET available_at='2000-01-01T00:00:00+00:00' WHERE slot_key=?",(key,))
            ids.append(submit_pending(store,key))
    with store.control() as conn:
        status=conn.execute('SELECT status FROM scheduled_slots WHERE slot_key=?',(key,)).fetchone()[0]
    assert status=='held' and len(set(ids))==4
    print(json.dumps(dict(lost_response_same_id=True,attempts=ids,status=status,schedules_inactive=True)))
'''
    result=command(['run','--rm','--no-deps','updater-worker','python','-c',script])
    (EVIDENCE/'scheduler-recovery.json').write_text(result)
    command(['start','updater-worker'])
    data_root=(EVIDENCE/'stack-data-root.txt').read_text()
    before=command(['exec','-T','updater-worker','python','-m','src.updater.cli','--data-dir',data_root,'current'])
    command(['restart','prefect-server','updater-worker'])
    after=command(['exec','-T','updater-worker','python','-m','src.updater.cli','--data-dir',data_root,'current'])
    assert before==after
    script='''
from pathlib import Path
import json,shutil
from uuid import uuid4
from src.updater.store import Store,StoreError
from src.updater.maintenance import backup,restore
from src.updater.reader import catalog_manifest,open_published_snapshot
from src.updater.locks import maintenance_active
store=Store(Path(DATA_ROOT))
original=catalog_manifest(store)
location=Path('/data/backup-'+uuid4().hex)
backup(store,location)
broken=location.with_name(location.name+'-corrupt')
shutil.copytree(location,broken)
with (broken/original.snapshot_path).open('ab') as handle: handle.write(b'bad')
try:
    restore(store,broken)
except StoreError as error:
    assert error.code=='backup_hash_mismatch'
else:
    raise RuntimeError('corrupt backup accepted')
restore(store,location)
current=catalog_manifest(store)
assert current==original and maintenance_active(store.root)
with open_published_snapshot(current,root=store.root) as reader:
    assert reader.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
print(json.dumps(dict(backup=str(location),manifest=current.model_dump(),restored=True,maintenance_gate=True,corrupt_rejected=True)))
'''.replace('DATA_ROOT',repr(data_root))
    output=command(['exec','-T','updater-worker','python','-c',script])
    record=json.loads(output.strip().splitlines()[-1])
    record['restart_preserved_current']=True
    (EVIDENCE/'backup-restore.json').write_text(json.dumps(record,indent=2))
    command(['cp','updater-worker:'+record['backup'],str(EVIDENCE/'business-backup')])
    print(json.dumps(dict(scheduler_recovery=True,restart_preserved_current=True,backup_restore=True)))


if __name__=='__main__':
    main()
