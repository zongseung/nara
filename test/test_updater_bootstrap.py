import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from httpx import HTTPStatusError, Request, Response
from prefect.exceptions import ObjectNotFound

from src.updater.bootstrap import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_new_installation_starts_paused_with_inactive_schedule(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {'NARA_DATA_DIR': temporary}), \
             patch('src.updater.bootstrap.get_client') as get_client:
            client = get_client.return_value.__enter__.return_value
            request = Request('GET', 'http://prefect-server:4200/api/deployments/name/test')
            client.read_deployment_by_name.side_effect = ObjectNotFound(
                http_exc=HTTPStatusError('missing', request=request, response=Response(404)))
            expected = uuid4()
            client.create_deployment.return_value = expected
            self.assertEqual(bootstrap(), str(expected))
            arguments = client.create_deployment.call_args.kwargs
            self.assertTrue(arguments['paused'])
            self.assertFalse(arguments['schedules'][0].active)
            self.assertEqual(arguments['concurrency_limit'], 1)
            self.assertEqual(arguments['entrypoint'], 'src/updater/flows.py:refresh_shop_prices')

    def test_restart_preserves_operator_activation_and_updates_code_location(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {'NARA_DATA_DIR': temporary}), \
             patch('src.updater.bootstrap.get_client') as get_client:
            client = get_client.return_value.__enter__.return_value
            expected = uuid4()
            client.read_deployment_by_name.return_value = SimpleNamespace(id=expected)
            self.assertEqual(bootstrap(), str(expected))
            client.create_deployment.assert_not_called()
            deployment_id, update = client.update_deployment.call_args.args
            self.assertEqual(deployment_id, expected)
            self.assertEqual(update.model_fields_set, {'path', 'entrypoint'})
            self.assertEqual(update.path, '/app')
            self.assertEqual(update.entrypoint, 'src/updater/flows.py:refresh_shop_prices')
