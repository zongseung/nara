"""Kill only the isolated QA worker and explicitly resume its persisted business run."""
import json
from pathlib import Path
import time
from uuid import uuid4
from test.integration.test_price_updater_stack import command,EVIDENCE


def main():
    root='/data/crash-'+uuid4().hex
    command(['exec','-T','fixture-api','python','-c',"from pathlib import Path; Path('/fixture/state.json').write_text('{\"price\":\"100\",\"count\":101,\"delay_seconds\":5}')"])
    script='''
from datetime import datetime,timezone
from pathlib import Path
import json
from src.updater.store import Store
from src.updater.dispatcher import ensure_slot,submit_pending
store=Store(Path(ROOT))
key=ensure_slot(store,datetime.now(timezone.utc),manual=True)
run=submit_pending(store,key,parameters=dict(environment='fixture',source_endpoint='http://fixture-api:8080/search',data_root=ROOT))
print(json.dumps(dict(slot=key,prefect_run=run)))
'''.replace('ROOT',repr(root))
    submitted=json.loads(command(['exec','-T','updater-worker','python','-c',script]).strip().splitlines()[-1])
    probe='''
from src.updater.store import Store,database
from pathlib import Path
import json
store=Store(Path(ROOT))
with store.control() as conn:
    row=conn.execute('SELECT business_run_id FROM scheduled_slots WHERE slot_key=?',(SLOT,)).fetchone()
    run=row[0]
count=0
if run and store.path('stage/'+run+'.sqlite').exists():
    with database(store.path('stage/'+run+'.sqlite')) as stage:
        count=stage.execute('SELECT count(*) FROM pages').fetchone()[0]
print(json.dumps(dict(business_run=run,pages=count)))
'''.replace('ROOT',repr(root)).replace('SLOT',repr(submitted['slot']))
    for _ in range(40):
        progress=json.loads(command(['exec','-T','updater-worker','python','-c',probe]))
        if progress['pages']==1:
            break
        time.sleep(1)
    else:
        raise RuntimeError('checkpoint window not observed')
    command(['kill','updater-worker'])
    command(['start','updater-worker'])
    command(['exec','-T','fixture-api','python','-c',"from pathlib import Path; Path('/fixture/state.json').write_text('{\"price\":\"100\",\"count\":101}')"])
    script='''
import json,time
from uuid import UUID,uuid4
from prefect.client.orchestration import get_client
from prefect.states import Crashed
from src.updater.dispatcher import DEPLOYMENT_NAME
with get_client(sync_client=True) as client:
    client.set_flow_run_state(UUID(OLD_RUN),Crashed(message='isolated QA worker SIGKILL'),force=True)
    time.sleep(3)
    deployment=client.read_deployment_by_name(DEPLOYMENT_NAME)
    run=client.create_flow_run_from_deployment(deployment.id,parameters=dict(slot_key=SLOT,environment='fixture',source_endpoint='http://fixture-api:8080/search',data_root=ROOT),idempotency_key='explicit-resume-'+uuid4().hex)
    for _ in range(60):
        run=client.read_flow_run(run.id)
        if run.state.is_final(): break
        time.sleep(2)
    assert run.state.is_completed(),str(run.state)
from src.updater.store import Store
from src.updater.reader import catalog_manifest,open_published_snapshot
from pathlib import Path
store=Store(Path(ROOT))
manifest=catalog_manifest(store)
assert manifest.source_refresh_run_id==BUSINESS
with open_published_snapshot(manifest,root=store.root) as reader:
    assert reader.connection.execute('SELECT count(*) FROM item_versions').fetchone()[0]==101
    assert reader.connection.execute('SELECT count(*) FROM price_history').fetchone()[0]==101
print(json.dumps(dict(old_prefect_run=OLD_RUN,new_prefect_run=str(run.id),same_business_run=BUSINESS,checkpoint_pages_before_kill=1,items=101,state=run.state.type.value)))
'''.replace('OLD_RUN',repr(submitted['prefect_run'])).replace('SLOT',repr(submitted['slot'])).replace('ROOT',repr(root)).replace('BUSINESS',repr(progress['business_run']))
    output=command(['exec','-T','updater-worker','python','-c',script])
    record=json.loads(output.strip().splitlines()[-1])
    (EVIDENCE/'worker-kill-resume.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))


if __name__=='__main__':
    main()
