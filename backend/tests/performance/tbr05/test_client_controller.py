import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('client_controller', Path(__file__).with_name('07_tbr05_sharded_load.py'))
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ClientLifecycleTests(unittest.TestCase):
    def test_failure_stops_actual_worker_behind_a_venv_launcher(self):
        import psutil
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / 'worker.pid'
            sleeper = ('import os,time;from pathlib import Path;'
                       f'Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(20)')
            failure = ('import time;from pathlib import Path;'
                       f'p=Path({str(pid_file)!r})\n'
                       'while not p.exists():time.sleep(.01)\nraise SystemExit(3)')
            try:
                with self.assertRaisesRegex(RuntimeError, 'failed'):
                    controller.run_clients([[sys.executable, '-c', sleeper],
                                            [sys.executable, '-c', failure]],
                                           Path(folder) / 'run', time.perf_counter() + 10)
                worker = int(pid_file.read_text())
                self.assertFalse(psutil.pid_exists(worker) and
                                 psutil.Process(worker).status() != psutil.STATUS_ZOMBIE,
                                 'actual worker survived its launcher')
            finally:
                if pid_file.exists():
                    try:
                        process = psutil.Process(int(pid_file.read_text()))
                        process.terminate()
                        psutil.wait_procs([process], timeout=3)
                    except psutil.NoSuchProcess:
                        pass

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


class ClientResourceTests(unittest.TestCase):
    class Missing(Exception):
        pass

    class Denied(Exception):
        pass

    def process(self, pid, birth, rss, cpu, children=(), failure=None):
        def memory_info():
            if failure:
                raise failure
            return types.SimpleNamespace(rss=rss)
        return types.SimpleNamespace(
            pid=pid, create_time=lambda: birth, memory_info=memory_info,
            cpu_times=lambda: types.SimpleNamespace(user=cpu, system=0),
            children=lambda recursive: list(children))

    def api(self, processes):
        def process(pid):
            if pid not in processes:
                raise self.Missing()
            return processes[pid]
        return types.SimpleNamespace(Process=process, NoSuchProcess=self.Missing, AccessDenied=self.Denied)

    def test_launcher_descendants_are_included_once_when_roots_overlap(self):
        worker = self.process(3, 30, 32000000, 2)
        launcher = self.process(2, 20, 1000, .01, [worker])
        parent = self.process(1, 10, 2000, .5, [launcher, worker])
        cache = {}
        row = controller.sample_client_processes(self.api({1: parent, 2: launcher}), [1, 2], cache)
        self.assertEqual(32003000, row['load_rss_bytes'])
        self.assertAlmostEqual(2.51, row['load_cpu_seconds'])
        self.assertEqual(3, row['load_process_count'])
        self.assertEqual([1, 2, 3], [p['pid'] for p in row['load_processes']])
        self.assertTrue(row['load_resource_complete'])

    def test_pid_reuse_keeps_separate_cpu_lifetimes_and_only_live_rss(self):
        cache = {}
        controller.sample_client_processes(self.api({1: self.process(1, 10, 100, 4)}), [1], cache)
        row = controller.sample_client_processes(self.api({1: self.process(1, 20, 200, 1)}), [1], cache)
        self.assertEqual(5, row['load_cpu_seconds'])
        self.assertEqual(200, row['load_rss_bytes'])
        self.assertEqual(20, row['load_processes'][0]['create_time'])

    def test_disappearing_and_denied_processes_are_explicitly_incomplete(self):
        gone = self.process(3, 30, 0, 0, failure=self.Missing())
        denied = self.process(2, 20, 0, 0, failure=self.Denied())
        parent = self.process(1, 10, 100, 1, [denied, gone])
        row = controller.sample_client_processes(self.api({1: parent}), [1, 9], {})
        self.assertEqual(100, row['load_rss_bytes'])
        self.assertFalse(row['load_resource_complete'])
        self.assertEqual({2, 3, 9}, {e['pid'] for e in row['load_resource_errors']})


if __name__ == '__main__':
    unittest.main()
