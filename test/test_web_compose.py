import unittest
from pathlib import Path

import yaml


class WebComposeTests(unittest.TestCase):
    def test_private_components_share_files_and_wait_for_registration(self):
        config = yaml.safe_load(Path('docker/compose.web.yaml').read_text())
        services = config['services']
        for name in ('web-api', 'web-worker', 'web-bootstrap', 'web-cache'):
            self.assertNotIn('ports', services[name])
        for name in ('web-api', 'web-worker'):
            self.assertEqual(services[name]['depends_on']['web-bootstrap']['condition'], 'service_completed_successfully')
            self.assertEqual(services[name]['volumes'], services['web-bootstrap']['volumes'])
        self.assertIn('127.0.0.1', services['web-gateway']['ports'][0])
        self.assertNotIn('updater-dispatcher', services)

    def test_disposable_cache_does_not_evict_prefect_queue_data(self):
        config = yaml.safe_load(Path('docker/compose.web.yaml').read_text())
        cache = config['services']['web-cache']
        self.assertIn('allkeys-lru', cache['command'])
        self.assertIn('--maxmemory', cache['command'])
        self.assertNotIn('volumes', cache)
        self.assertEqual(config['services']['redis']['extends']['service'], 'redis')

    def test_proxy_preserves_origin_port_and_does_not_cache_jobs(self):
        config = Path('docker/nginx.web.conf').read_text()
        self.assertIn('proxy_set_header Host $http_host;', config)
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme;', config)
        self.assertIn('proxy_cache off;', config)
        self.assertIn('client_max_body_size 8m;', config)
