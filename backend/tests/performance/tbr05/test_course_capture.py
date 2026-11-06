import hashlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[4] / 'docs/10_验收证据/19_后端综合验收/检查脚本/11_课程口径HTTP负载.py'
SPEC = importlib.util.spec_from_file_location('course_capture', SCRIPT)
load = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests'), 'psutil': types.ModuleType('psutil')}):
    SPEC.loader.exec_module(load)


class CourseExportEvidenceTests(unittest.TestCase):
    def capture(self, kind='detail_export', record_count='1', reference='f56bdb04-97ba-421b-815e-4196830cbad9'):
        body = b'column\r\nvalue\r\n'
        headers = {'Content-Length': str(len(body)), 'X-Byte-Count': str(len(body)),
                   'X-Content-SHA256': hashlib.sha256(body).hexdigest(),
                   'X-Record-Count': record_count, 'X-Export-Ref': reference}
        response = types.SimpleNamespace(status_code=200, headers=headers, iter_content=lambda **kw: [body])
        session = types.SimpleNamespace(cookies={'csrftoken': 'synthetic'}, request=lambda *a, **kw: response)
        spec = (kind, 'POST', '/synthetic', {}, '1-20', 1)
        with patch.object(load, 'request_spec', return_value=spec):
            return load.request_one(session, {}, 'http://synthetic', 1, None, {}, 0)

    def test_export_keeps_server_reference_and_counts_for_exact_audit(self):
        row = self.capture()
        self.assertTrue(row['ok'])
        self.assertEqual('f56bdb04-97ba-421b-815e-4196830cbad9', row['server_ref'])
        self.assertEqual(1, row['record_count'])
        self.assertEqual(1, row['expected_count'])
        self.assertEqual(row['bytes'], row['content_length'])

    def test_detail_count_mismatch_rejected_despite_valid_digest(self):
        self.assertFalse(self.capture(record_count='2')['ok'])

    def test_missing_or_invalid_export_reference_rejected(self):
        for reference in ('', 'not-a-uuid'):
            with self.subTest(reference=reference):
                self.assertFalse(self.capture(reference=reference)['ok'])


if __name__ == '__main__':
    unittest.main()
