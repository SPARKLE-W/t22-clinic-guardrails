import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('client_controller', Path(__file__).with_name('07_tbr05_sharded_load.py'))
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ClientLifecycleTests(unittest.TestCase):
    def test_success_collects_all_children_and_preserves_stdout(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'run'
            states = controller.run_clients([[sys.executable, '-c', 'print("child")']] * 4,
                                            prefix, time.perf_counter() + 10)
            self.assertEqual([0] * 4, [r['returncode'] for r in states])
            self.assertEqual(4, len({r['pid'] for r in states}))
            self.assertEqual('child', (Path(folder) / 'run-shard-0.stdout.log').read_text().strip())
            self.assertEqual(states, json.loads(prefix.with_suffix('.children.json').read_text()))

    def test_failure_terminates_sibling_and_keeps_failure_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'run'
            commands = [[sys.executable, '-c', 'import time;time.sleep(20)'],
                        [sys.executable, '-c', 'raise SystemExit(3)']]
            before = time.perf_counter()
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                controller.run_clients(commands, prefix, before + 10)
            self.assertLess(time.perf_counter() - before, 8)
            states = json.loads(prefix.with_suffix('.children.json').read_text())
            self.assertTrue(all(r['returncode'] is not None for r in states))
            self.assertEqual(3, states[1]['returncode'])


if __name__ == '__main__':
    unittest.main()
