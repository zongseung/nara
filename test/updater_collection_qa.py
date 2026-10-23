import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread

from src.updater.dispatcher import ensure_slot
from src.updater.flows import refresh_shop_prices
from src.updater.reader import catalog_manifest, open_published_snapshot
from src.updater.store import Store
from datetime import datetime, timezone


def main() -> None:
    products = [dict(ctrtItemMngNo=str(i), itemIdnfNo='goods1', itemIdnfNm='sample',
                     itemIndfNmView='sample', ctrtUprc='100', ctrtUntVal='EA',
                     ctentUntyGrpNm='Seller', mnftrEtpsNm='Maker', totCnt=1301)
                for i in range(1301)]
    calls: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            query = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['searchVO']
            number, size = int(query['currentPage']), int(query['recordCountPerPage'])
            calls.append(number)
            start = (number-1)*size
            wire = json.dumps(dict(rsltList=products[start:start+size], totalSize=1301,
                                   metadata='original response ' * 10000)).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            slot = ensure_slot(store, datetime.now(timezone.utc), manual=True)
            result = refresh_shop_prices(slot_key=slot, environment='fixture',
                                        source_endpoint=f'http://127.0.0.1:{server.server_port}/search',
                                        data_root=directory)
            manifest = catalog_manifest(store)
            with open_published_snapshot(manifest, root=store.root) as snapshot:
                count = snapshot.connection.execute('SELECT count(*) FROM items_current').fetchone()[0]
            if count != 1301 or result['status'] != 'published':
                raise RuntimeError('fixture_flow_did_not_publish_all_rows')
            print(json.dumps(dict(result=result, rows=count, source_pages=calls,
                                  environment=manifest.environment)), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
