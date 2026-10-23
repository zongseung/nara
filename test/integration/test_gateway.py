import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


@unittest.skipUnless(os.environ.get('NARA_GATEWAY_QA') == '1', 'explicit isolated gateway QA only')
class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = Path(os.environ['NARA_GATEWAY_QA_ENV_FILE']).read_text().splitlines()
        cls.auth = next(line.split('=', 1)[1] for line in env if line.startswith('PREFECT_AUTH_STRING='))
        cls.base = os.environ.get('NARA_GATEWAY_QA_URL', 'http://127.0.0.1:14201')
        cls.headers = {'Authorization': 'Basic ' + base64.b64encode(cls.auth.encode()).decode(),
                       'Content-Type': 'application/json'}

    def test_authentication_and_same_origin_ui(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.base + '/api/variables/filter', data=b'{}',
                            headers={'Content-Type': 'application/json'}), timeout=10)
        self.assertEqual(caught.exception.code, 401)
        with urlopen(Request(self.base + '/api/variables/filter', data=b'{}', headers=self.headers), timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with urlopen(self.base + '/ui-settings', timeout=10) as response:
            settings = json.load(response)
            self.assertEqual(settings['api_url'], '/api')
            self.assertEqual(settings['auth'], 'BASIC')

    def test_mutation_is_visible_without_cached_response(self):
        name = 'gateway-qa-' + uuid4().hex
        endpoint = self.base + '/api/variables/name/' + name
        with urlopen(Request(self.base + '/api/variables/',
                            data=json.dumps({'name': name, 'value': '100'}).encode(),
                            headers=self.headers), timeout=10) as response:
            self.assertEqual(response.status, 201)
        try:
            for value in ('100', '120', '100'):
                with urlopen(Request(endpoint, data=json.dumps({'value': value}).encode(),
                                    method='PATCH', headers=self.headers), timeout=10) as response:
                    self.assertEqual(response.status, 204)
                with urlopen(Request(endpoint, headers=self.headers), timeout=10) as response:
                    self.assertEqual(json.load(response)['value'], value)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
        finally:
            with urlopen(Request(endpoint, method='DELETE', headers=self.headers), timeout=10) as response:
                self.assertEqual(response.status, 204)

    def test_websocket_upgrade_preserves_prefect_authentication(self):
        from websockets.exceptions import ConnectionClosedError
        from websockets.sync.client import connect

        endpoint = self.base.replace('http://', 'ws://', 1) + '/api/events/in'
        with connect(endpoint, subprotocols=['prefect'], open_timeout=10) as socket:
            socket.send(json.dumps({'type': 'auth', 'token': self.auth}))
            self.assertEqual(json.loads(socket.recv(timeout=10)), {'type': 'auth_success'})
        with connect(endpoint, subprotocols=['prefect'], open_timeout=10) as socket:
            socket.send(json.dumps({'type': 'auth', 'token': 'incorrect-qa-token'}))
            with self.assertRaises(ConnectionClosedError):
                socket.recv(timeout=10)

    def test_request_body_limit(self):
        request = Request(self.base + '/api/variables/filter', data=b' ' * (8 * 1024 * 1024 + 1),
                          headers=self.headers)
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 413)

    def test_z_burst_is_bounded_and_health_remains_available(self):
        def status(_):
            try:
                with urlopen(Request(self.base + '/api/variables/filter', data=b'{}',
                                     headers=self.headers), timeout=10) as response:
                    return response.status
            except HTTPError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=16) as pool:
            counts = Counter(pool.map(status, range(200)))
        self.assertGreater(counts[429], 0)
        self.assertLessEqual(set(counts), {200, 429})
        with urlopen(self.base + '/api/health', timeout=10) as response:
            self.assertEqual(response.status, 200)
