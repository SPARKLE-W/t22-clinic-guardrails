import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SETTINGS = Path(__file__).resolve().parents[1] / 'vetapp' / 'settings.py'


class AllowedHostSettingsTests(unittest.TestCase):
    def load_allowed_hosts(self, extra_hosts=None):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'app.json'
            config.write_text(json.dumps({'secret_key': 'test', 'database': {'NAME': 't22_app'}}),
                              encoding='utf-8')
            environment = {'T22_APP_CONFIG': str(config)}
            if extra_hosts is None:
                environment['T22_ALLOWED_HOSTS'] = ''
            else:
                environment['T22_ALLOWED_HOSTS'] = extra_hosts
            with patch.dict(os.environ, environment):
                return runpy.run_path(str(SETTINGS))['ALLOWED_HOSTS']

    def test_default_hosts_remain_loopback_and_testserver(self):
        self.assertEqual(['127.0.0.1', 'localhost', 'testserver'], self.load_allowed_hosts())

    def test_explicit_hosts_are_added_without_empty_entries(self):
        self.assertEqual(['127.0.0.1', 'localhost', 'testserver', '172.22.138.131', 'clinic.internal'],
                         self.load_allowed_hosts(' 172.22.138.131, , clinic.internal '))
