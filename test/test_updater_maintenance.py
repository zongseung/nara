from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from src.updater.collector import refresh,CollectionPolicy
from src.updater.locks import maintenance_active
from src.updater.maintenance import backup,restore,resume,prune_history,prune_files,freshness
from src.updater.reader import catalog_manifest,open_published_snapshot
from src.updater.store import Store,StoreError
from test.updater_fixtures import FixtureSource,row


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'data')

    def publish(self,price='100',tick=0):
        return refresh(self.store,FixtureSource([row(price=price)],tick),policy=CollectionPolicy(request_interval=0),environment='fixture')

    def test_backup_restore_hash_and_gate(self):
        first=self.publish()
        location=backup(self.store,self.root/'backup')
        self.assertTrue(maintenance_active(self.store.root))
        resume(self.store)
        self.publish('200',1)
        restore(self.store,location)
        self.assertEqual(catalog_manifest(self.store).publication_id,first.publication_id)
        with self.assertRaises(StoreError):
            with open_published_snapshot(first,root=self.store.root,require_active=True):
                self.fail('maintenance gate bypassed')
        with open_published_snapshot(first,root=self.store.root) as reader:
            self.assertEqual(list(reader.current())[0]['contract_price_decimal'],'100')

    def test_restore_refuses_live_pin(self):
        first=self.publish()
        location=backup(self.store,self.root/'backup')
        with open_published_snapshot(first,root=self.store.root):
            with self.assertRaises(StoreError):
                restore(self.store,location)

    def test_maintenance_keeps_source_time_and_baseline(self):
        for index,price in enumerate(('100','120','130')):
            original=self.publish(price,index)
        now=datetime.fromisoformat(original.observed_to)+timedelta(days=400)
        manifest=prune_history(self.store,now=now)
        self.assertEqual(manifest.source_refresh_run_id,original.source_refresh_run_id)
        self.assertEqual(manifest.observed_from,original.observed_from)
        self.assertEqual(freshness(manifest,now)['status'],'stale')
        with open_published_snapshot(manifest,root=self.store.root) as reader:
            self.assertEqual(reader.connection.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(reader.connection.execute('SELECT count(*) FROM item_versions').fetchone()[0],1)
            self.assertEqual(list(reader.current())[0]['contract_price_decimal'],'130')

    def test_three_snapshots_plus_pinned_and_36_72_hour_thresholds(self):
        first=self.publish()
        with open_published_snapshot(first,root=self.store.root):
            for tick in range(1,5):
                self.publish(str(100+tick),tick)
            removed=prune_files(self.store)['removed']
            self.assertNotIn(Path(first.snapshot_path).name,removed)
        removed=prune_files(self.store)['removed']
        self.assertIn(Path(first.snapshot_path).name,removed)
        time=datetime.fromisoformat(first.observed_from)
        self.assertEqual(freshness(first,time+timedelta(hours=37))['status'],'warning')
        self.assertEqual(freshness(first,time+timedelta(hours=73))['status'],'stale')

    def test_backup_preserves_extant_rollback_snapshots(self):
        for tick in range(4):
            self.publish(str(100+tick),tick)
        location=backup(self.store,self.root/'backup')
        import json
        bundle=json.loads((location/'backup.json').read_text())
        self.assertEqual(len(bundle['snapshots']),4)
        self.assertTrue(all((location/item['path']).is_file() for item in bundle['snapshots']))

    def test_resume_refuses_corrupt_current_and_keeps_gate(self):
        manifest=self.publish()
        backup(self.store,self.root/'backup')
        with self.store.path(manifest.snapshot_path).open('ab') as handle:
            handle.write(b'corrupt')
        with self.assertRaises(StoreError):
            resume(self.store)
        self.assertTrue(maintenance_active(self.store.root))

    def test_backup_refuses_missing_active_snapshot(self):
        manifest=self.publish()
        self.store.path(manifest.snapshot_path).unlink()
        destination=self.root/'missing-backup'
        with self.assertRaises(StoreError):
            backup(self.store,destination)
        self.assertFalse((destination/'backup.json').exists())

    def test_restore_refuses_bundle_omitting_active_snapshot_before_pointer_change(self):
        import json
        first=self.publish()
        location=backup(self.store,self.root/'backup')
        record=json.loads((location/'backup.json').read_text())
        record['snapshots']=[]
        (location/'backup.json').write_text(json.dumps(record))
        latest=self.publish('200',1)
        with self.assertRaises(StoreError):
            restore(self.store,location)
        self.assertEqual(catalog_manifest(self.store).publication_id,latest.publication_id)
        self.assertNotEqual(latest.publication_id,first.publication_id)

    def test_backup_refuses_corrupt_active_snapshot(self):
        manifest=self.publish()
        with self.store.path(manifest.snapshot_path).open('ab') as handle:
            handle.write(b'corruption')
        destination=self.root/'corrupt-backup'
        with self.assertRaises(StoreError) as caught:
            backup(self.store,destination)
        self.assertEqual(caught.exception.code,'backup_hash_mismatch')
        self.assertFalse((destination/'backup.json').exists())

    def test_restore_refuses_snapshot_digest_disagreeing_with_control(self):
        import json
        manifest=self.publish()
        location=backup(self.store,self.root/'backup')
        record=json.loads((location/'backup.json').read_text())
        record['snapshots'][0]['sha256']='0'*64
        (location/'backup.json').write_text(json.dumps(record))
        with self.assertRaises(StoreError) as caught:
            restore(self.store,location)
        self.assertEqual(caught.exception.code,'backup_manifest_mismatch')
        self.assertEqual(catalog_manifest(self.store).publication_id,manifest.publication_id)
