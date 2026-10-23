from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from core.catalog_sources import get_profile, SourceError
from src.g2b_client import fetch_search_page
from src.updater.collector import refresh, CollectionPolicy
from src.updater.reader import catalog_manifest, open_published_snapshot
from src.updater.store import Store, StoreError
from test.updater_fixtures import FixtureSource, row


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.policy = CollectionPolicy(page_size=1, safe_pages=3, request_interval=0)

    def test_short_drift_old_window_and_nonfinite_never_publish(self):
        for fault in ('short', 'count', 'window'):
            class BadSource(FixtureSource):
                def fetch(inner, query):
                    page = super(BadSource, inner).fetch(query)
                    if query.page == 2:
                        if fault == 'short':
                            return replace(page, rows=())
                        if fault == 'count':
                            return replace(page, total_size=3)
                        return replace(page, observed_at=page.observed_at + timedelta(hours=13))
                    return page
            with self.assertRaises(StoreError):
                refresh(self.store, BadSource([row(),row('B')]), policy=self.policy, environment='fixture')
        for price in ('NaN','Infinity','-1'):
            with self.assertRaises(SourceError):
                refresh(self.store, FixtureSource([row(price=price)]), policy=self.policy, environment='fixture')
        with self.assertRaises(StoreError):
            catalog_manifest(self.store)

    def test_real_http_fixture_transport(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner):
                query = json.loads(inner.rfile.read(int(inner.headers['Content-Length'])))
                self.assertEqual(query['searchVO']['prchsMthdSeCd'], '')
                payload = json.dumps(dict(rsltList=[dict(row(),totCnt=1)],totalSize=1)).encode()
                inner.send_response(200)
                inner.end_headers()
                inner.wfile.write(payload)
            def log_message(self, *args):
                return
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = replace(get_profile('shopping-all'), endpoint=f'http://127.0.0.1:{server.server_port}/search')
        class HttpSource:
            def fetch(self, query):
                return fetch_search_page(query, profile, timeout=2)
        try:
            manifest = refresh(self.store, HttpSource(), profile=profile, policy=self.policy, environment='fixture')
            with open_published_snapshot(manifest, root=self.store.root) as reader:
                self.assertEqual(len(list(reader.current())), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_process_crashes_before_file_before_cas_after_cas(self):
        original = refresh(self.store, FixtureSource([row()]), policy=self.policy, environment='fixture')
        script = '''
import os,sys,json
from pathlib import Path
from unittest.mock import patch
from core.catalog_sources import get_profile
from test.updater_fixtures import FixtureSource,row
from src.updater.collector import collect,CollectionPolicy
from src.updater.publisher import publish
from src.updater.store import Store
store=Store(Path(sys.argv[1]))
mode=sys.argv[2]
with store.writer(get_profile('shopping-all'),environment='fixture') as lease:
    collect(store,lease,FixtureSource([row(price='120')],1),policy=CollectionPolicy(request_interval=0))
    Path(sys.argv[1],'crash-run.json').write_text(json.dumps(dict(run_id=lease.run_id,owner=lease.owner,fence=lease.fence,previous_run_id=lease.previous_run_id)))
    if mode=='before_file':
        with patch('src.updater.publisher.os.replace', side_effect=lambda *a:os._exit(71)):
            publish(store,lease)
    elif mode=='before_cas':
        original=Store.check_owner
        calls=[0]
        def check(self,conn,lease):
            calls[0]+=1
            if calls[0]==2: os._exit(72)
            return original(self,conn,lease)
        with patch.object(Store,'check_owner',check): publish(store,lease)
    else:
        publish(store,lease)
        os._exit(73)
'''
        for mode, code in (('before_file',71),('before_cas',72),('after_cas',73)):
            completed = subprocess.run([sys.executable,'-c',script,str(self.store.root),mode], capture_output=True, text=True)
            self.assertEqual(completed.returncode, code, completed.stderr)
            current = catalog_manifest(self.store)
            if mode != 'after_cas':
                self.assertEqual(current, original)
            else:
                self.assertNotEqual(current.publication_id, original.publication_id)
                with open_published_snapshot(current, root=self.store.root) as reader:
                    self.assertEqual(reader.connection.execute('SELECT count(*) FROM price_history').fetchone()[0], 2)
