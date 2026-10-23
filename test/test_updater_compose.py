from pathlib import Path
import unittest
import yaml


class ComposeTests(unittest.TestCase):
    def test_startup_registers_work_before_worker_and_dispatcher(self):
        config = yaml.safe_load(Path('docker/compose.yaml').read_text())
        services = config['services']
        bootstrap = services['updater-bootstrap']
        self.assertNotIn('profiles', bootstrap)
        self.assertEqual(bootstrap['restart'], 'no')
        for name in ('updater-worker', 'updater-dispatcher'):
            self.assertEqual(services[name]['depends_on']['updater-bootstrap']['condition'],
                             'service_completed_successfully')

    def test_private_databases_fixed_versions_and_no_socket(self):
        content=Path('docker/compose.yaml').read_text()
        config=yaml.safe_load(content)
        self.assertNotIn('docker.sock',content)
        self.assertNotIn('ports',config['services']['postgres'])
        self.assertNotIn('ports',config['services']['redis'])
        self.assertNotIn('ports',config['services']['prefect-server'])
        self.assertIn('127.0.0.1', config['services']['gateway']['ports'][0])
        self.assertIn('sha256:',config['services']['postgres']['image'])
        for name in ('prefect-server','prefect-services','updater-worker'):
            self.assertEqual(config['services'][name]['image'],'nara-updater:prefect-3.9.0')
        self.assertIn('prefect==3.9.0',Path('docker/requirements-updater.lock').read_text())

    def test_event_bus_and_containers_have_bounded_resources(self):
        config = yaml.safe_load(Path('docker/compose.yaml').read_text())
        services = config['services']
        command = services['redis']['command']
        self.assertIn('--maxmemory', command)
        self.assertEqual(command[command.index('--maxmemory-policy') + 1], 'noeviction')
        self.assertIn('--appendfsync', command)
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertIn('mem_limit', service)
                self.assertIn('memswap_limit', service)
                self.assertIn('pids_limit', service)
                self.assertEqual(service['logging']['driver'], 'local')
                self.assertIn('max-size', service['logging']['options'])
                self.assertIn('max-file', service['logging']['options'])

    def test_gateway_preserves_private_upstream_and_browser_api_origin(self):
        config = yaml.safe_load(Path('docker/compose.yaml').read_text())
        services = config['services']
        self.assertEqual(services['prefect-server']['environment']['PREFECT_SERVER_UI_API_URL'], '/api')
        self.assertIn('sha256:', Path('docker/nginx.Dockerfile').read_text())
        self.assertTrue(services['gateway']['read_only'])
        self.assertTrue(all(mount.startswith('/') for mount in services['gateway']['tmpfs']))
        self.assertNotIn('ports', services['redis'])
        self.assertNotIn('ports', services['postgres'])
