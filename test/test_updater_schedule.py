from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from core.catalog_sources import get_profile
from test.updater_fixtures import FixtureSource,row
from src.updater.collector import collect,CollectionPolicy
from src.updater.dispatcher import initialize,ensure_slot,latest_due,claim_slot
from src.updater.publisher import publish
from src.updater.store import Store,StoreError


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name))
        initialize(self.store)

    def test_kst_latest_only_and_unique_slot_outbox(self):
        now=datetime(2026,10,23,16,tzinfo=timezone.utc)
        self.assertEqual(latest_due(now).isoformat(),'2026-10-22T17:00:00+00:00')
        key=ensure_slot(self.store,latest_due(now))
        self.assertEqual(key,ensure_slot(self.store,latest_due(now)))
        with self.store.control() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM scheduled_slots').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT count(*) FROM dispatch_outbox').fetchone()[0],1)

    def test_slot_success_is_atomic_with_publication(self):
        key=ensure_slot(self.store,datetime.now(timezone.utc),manual=True)
        with self.store.writer(get_profile('shopping-all'),environment='fixture') as lease:
            self.assertTrue(claim_slot(self.store,key,lease.run_id))
            lease=replace(lease,slot_key=key)
            collect(self.store,lease,FixtureSource([row()]),policy=CollectionPolicy(request_interval=0))
            publish(self.store,lease)
        with self.store.control() as conn:
            self.assertEqual(conn.execute('SELECT status FROM scheduled_slots').fetchone()[0],'published')
            self.assertEqual(conn.execute('SELECT count(*) FROM publications').fetchone()[0],1)

    def test_mismatched_slot_rolls_back_pointer_and_event(self):
        key=ensure_slot(self.store,datetime.now(timezone.utc),manual=True)
        with self.store.writer(get_profile('shopping-all'),environment='fixture') as lease:
            collect(self.store,lease,FixtureSource([row()]),policy=CollectionPolicy(request_interval=0))
            with self.assertRaises(StoreError):
                publish(self.store,replace(lease,slot_key=key))
        with self.store.control() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM publications').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM publication_events').fetchone()[0],0)

    def test_persisted_resume_new_fence_same_stage_and_policy_guard(self):
        policy=CollectionPolicy(request_interval=0)
        with self.store.writer(get_profile('shopping-all'),environment='fixture') as first:
            collect(self.store,first,FixtureSource([row()]),policy=policy)
        with self.store.writer(first.profile,environment='fixture',resume_run_id=first.run_id) as second:
            self.assertGreater(second.fence,first.fence)
            self.assertEqual(second.run_id,first.run_id)
            with self.assertRaises(StoreError):
                collect(self.store,second,FixtureSource([row()]),policy=replace(policy,page_size=1))
            source=FixtureSource([row()])
            collect(self.store,second,source,policy=policy)
            self.assertEqual(len(source.calls),2)
            publish(self.store,second)
