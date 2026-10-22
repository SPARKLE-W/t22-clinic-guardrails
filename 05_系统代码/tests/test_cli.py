import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_controls import demand

ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'vetcontrols', *map(str, args)], cwd=ROOT,
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True, encoding='utf-8')

    def test_compile_verify_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'demand.json'
            source.write_text(json.dumps(demand()), encoding='utf-8')
            compiled = root / 'compiled'
            result = self.run_cli('compile', '--input', source, '--output', compiled)
            self.assertEqual(0, result.returncode, result.stderr)
            candidate = compiled / 'candidate.json'
            saved = candidate.read_bytes()
            result = self.run_cli('verify', '--candidate', candidate, '--output', root / 'verified')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(json.loads((root / 'verified/report.json').read_text())['ok'])
            result = self.run_cli('compile', '--input', source, '--output', compiled)
            self.assertEqual(2, result.returncode)
            self.assertEqual(saved, candidate.read_bytes())

    def test_failed_compile_has_report_and_no_candidate_or_raw_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'bad.json'
            value = demand()
            del value['quality_gates']
            value['clinical_payload'] = {'diagnosis': 'DO-NOT-ECHO-CLINICAL-TEXT'}
            source.write_text(json.dumps(value), encoding='utf-8')
            result = self.run_cli('compile', '--input', source, '--output', root / 'failed')
            self.assertEqual(2, result.returncode)
            self.assertFalse((root / 'failed/candidate.json').exists())
            report = (root / 'failed/report.json').read_text(encoding='utf-8')
            self.assertFalse(json.loads(report)['ok'])
            self.assertNotIn('DO-NOT-ECHO-CLINICAL-TEXT', report + result.stdout + result.stderr)

    def test_unreadable_file_and_duplicate_keys_are_controlled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_cli('compile', '--input', root / 'missing.json', '--output', root / 'missing')
            self.assertEqual(2, result.returncode)
            self.assertFalse(json.loads((root / 'missing/report.json').read_text())['ok'])
            source = root / 'duplicate.json'
            source.write_text('{"a":1,"a":2}', encoding='utf-8')
            result = self.run_cli('verify', '--candidate', source, '--output', root / 'duplicate')
            self.assertEqual(2, result.returncode)
            self.assertFalse((root / 'duplicate/candidate.json').exists())


if __name__ == '__main__':
    unittest.main()
