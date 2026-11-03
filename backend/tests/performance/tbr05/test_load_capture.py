import hashlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name('03_tbr05_load.py')
SPEC = importlib.util.spec_from_file_location('tbr05_load', SCRIPT)
load = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests'),
                              'psutil': types.ModuleType('psutil')}):
    SPEC.loader.exec_module(load)


class Response:
    status_code = 200
    headers = {}

    def iter_content(self, chunk_size):
        yield b'{"items":[]}'

    def close(self):
        pass


class Session:
    def request(self, *args, **kwargs):
        return Response()


class LoadCaptureTests(unittest.TestCase):
    def test_http_inflight_ends_before_response_validation(self):
        with patch.object(load, 'select_request', return_value=('doctor', 'GET', '/api', None, 'json', None)):
            with patch.object(load.json, 'loads', side_effect=AssertionError('validated before HTTP completed')):
                capture = load.run_one(Session(), 'http://test', {'doctor': {}}, {}, 1, 'query', None)

        self.assertEqual(0, load.ACTIVE)
        self.assertLess(capture[0]['started'], capture[0]['ended'])
        self.assertTrue(load.validate_capture(capture)['ok'])

    def test_export_validation_checks_complete_bytes_after_http_timing(self):
        body = b'event_ref,record_ref\r\na,b\r\n'
        digest = hashlib.sha256(body).hexdigest()
        row = {'request_id': 2, 'kind': 'detail_export', 'status': 200,
               'expected_count': 1, 'started': 1.0, 'ended': 1.1, 'bytes': len(body)}
        headers = {'Content-Length': str(len(body)), 'X-Byte-Count': str(len(body)),
                   'X-Content-SHA256': digest, 'X-Record-Count': '1'}

        result = load.validate_capture((row, body, headers, None))

        self.assertTrue(result['ok'])
        self.assertEqual(1, result['rows'])
        self.assertEqual(1.1, result['ended'])


if __name__ == '__main__':
    unittest.main()
