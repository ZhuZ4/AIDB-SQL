"""Real Windows process tests; fixtures never import the agent or call an API."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from experiments import run_batch
from experiments.process_control import identity_alive, terminate_worker_tree, wait_worker_hello
from experiments.state import State, atomic_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r'''
import argparse, json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, ROOT_LITERAL)
from experiments.process_control import worker_handshake
assert 'sqlite3' not in sys.modules
p = argparse.ArgumentParser()
for name in ('input', 'output', 'dispatch-ready', 'worker-hello', 'usage-checkpoint'):
    p.add_argument('--' + name)
args = p.parse_args()
worker_handshake(Path(args.worker_hello), Path(args.dispatch_ready), timeout=5)
if args.input and json.loads(Path(args.input).read_text()).get('fixture_mode') == 'complete':
    Path(args.output).write_text(json.dumps({'status':'succeeded', 'submitted_final_sql':'SELECT 1', 'llm_calls':0}))
    raise SystemExit(0)
tree_path = Path(args.worker_hello + '.tree.json')
child_path = Path(args.worker_hello + '.child.json')
child_code = 'import json,os,time; from pathlib import Path; Path(' + repr(str(child_path)) + ').write_text(json.dumps({"pid":os.getpid()})); time.sleep(60)'
child = subprocess.Popen([sys.executable, '-c', child_code], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
until = time.monotonic() + 5
while not child_path.exists():
    if time.monotonic() > until:
        raise RuntimeError('fixture descendant did not start')
    time.sleep(.02)
tree_path.write_text(json.dumps({'actual':os.getpid(), 'child_launcher':child.pid, 'actual_child':json.loads(child_path.read_text())['pid']}))
time.sleep(60)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows venv redirector regression tests')
class WindowsWorkerProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.fixture = self.directory / 'process_fixture.py'
        self.fixture.write_text(FIXTURE.replace('ROOT_LITERAL', repr(str(ROOT))), encoding='utf-8')
        self.state = State(self.directory / 'state.sqlite')
        self.addCleanup(self.state.db.close)
        self.state.initialize('run', 'fixture', 'frozen', [{'question_id': 7, 'db_id': 'fixture'}])
        self.attempt = self.state.start('run', 7, 900)
        self.trace = self.directory / 'traces'
        self.trace.mkdir()
        self.hello = self.trace / '7.attempt1.hello.json'
        self.ready = self.trace / '7.attempt1.ready.json'
        self.launcher = None
        self.actual = None
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        if self.launcher is not None:
            if self.actual is None and self.hello.is_file():
                self.actual = json.loads(self.hello.read_text(encoding='utf-8'))
            terminate_worker_tree(self.launcher, self.actual)

    def wait_tree(self):
        path = Path(str(self.hello) + '.tree.json')
        deadline = time.monotonic() + 5
        while not path.is_file():
            if time.monotonic() >= deadline:
                self.fail('Fixture did not enter the registered processing stage')
            time.sleep(0.02)
        return json.loads(path.read_text(encoding='utf-8'))

    def launch_fixture(self, independent_launcher=False):
        command = [
            sys.executable, str(self.fixture), '--worker-hello', str(self.hello),
            '--dispatch-ready', str(self.ready),
        ]
        if independent_launcher:
            # CPython venv redirectors may use a kill-on-close Job object.
            # A real base-interpreter wrapper deterministically reproduces a
            # launcher exiting while its separately launched interpreter lives.
            wrapper = 'import subprocess,time; subprocess.Popen(' + repr(command) + '); time.sleep(60)'
            command = [sys._base_executable, '-c', wrapper]
        self.launcher = subprocess.Popen(command, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.actual = wait_worker_hello(self.hello, timeout=5)
        self.state.attach_worker('run', 7, self.actual['worker_pid'])
        atomic_json(self.ready, self.actual)
        return self.wait_tree()

    def test_timeout_kills_actual_interpreter_and_descendants(self):
        real_popen = subprocess.Popen

        def use_fixture(command, *args, **kwargs):
            if len(command) >= 3 and command[1:3] == ['-m', 'experiments.worker']:
                command = [command[0], str(self.fixture), *command[3:]]
                self.launcher = real_popen(command, *args, **kwargs)
                return self.launcher
            return real_popen(command, *args, **kwargs)

        with patch.object(run_batch.subprocess, 'Popen', side_effect=use_fixture):
            result = run_batch.run_worker({'question_id': 7}, self.directory, 3, self.state, 'run', 7, 1)
        self.actual = json.loads(self.hello.read_text(encoding='utf-8'))
        tree = self.wait_tree()
        self.assertNotEqual(self.launcher.pid, self.actual['worker_pid'], 'This regression fixture must exercise the venv redirector')
        self.assertEqual(self.state.rows('run')[0]['worker_pid'], self.actual['worker_pid'])
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['error_category'], 'timeout')
        self.assertFalse(identity_alive(self.actual))
        from experiments.process_control import process_identity
        self.assertIsNone(process_identity(tree['actual_child']))
        self.assertIsNone(process_identity(tree['child_launcher']))
        self.assertIsNotNone(self.launcher.poll())

    def test_normal_result_returns_after_actual_interpreter_finishes(self):
        real_popen = subprocess.Popen

        def use_fixture(command, *args, **kwargs):
            if len(command) >= 3 and command[1:3] == ['-m', 'experiments.worker']:
                command = [command[0], str(self.fixture), *command[3:]]
                self.launcher = real_popen(command, *args, **kwargs)
                return self.launcher
            return real_popen(command, *args, **kwargs)

        with patch.object(run_batch.subprocess, 'Popen', side_effect=use_fixture):
            result = run_batch.run_worker({'question_id': 7, 'fixture_mode': 'complete'},
                                         self.directory, 10, self.state, 'run', 7, 1)
        self.actual = json.loads(self.hello.read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['submitted_final_sql'], 'SELECT 1')
        self.assertEqual(result['llm_calls'], 0)
        self.assertEqual(self.state.rows('run')[0]['worker_pid'], self.actual['worker_pid'])
        self.assertFalse(identity_alive(self.actual))

    def test_unverified_cleanup_keeps_running_identity_and_blocks_recovery(self):
        real_popen = subprocess.Popen

        def use_fixture(command, *args, **kwargs):
            if len(command) >= 3 and command[1:3] == ['-m', 'experiments.worker']:
                command = [command[0], str(self.fixture), *command[3:]]
                self.launcher = real_popen(command, *args, **kwargs)
                return self.launcher
            return real_popen(command, *args, **kwargs)

        with patch.object(run_batch.subprocess, 'Popen', side_effect=use_fixture), \
                patch.object(run_batch, 'terminate_worker_tree', side_effect=RuntimeError('cleanup failed')):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                run_batch.run_worker({'question_id': 7}, self.directory, 1.5, self.state, 'run', 7, 1)
        self.actual = json.loads(self.hello.read_text(encoding='utf-8'))
        self.assertTrue(identity_alive(self.actual))
        self.assertEqual(self.state.rows('run')[0]['status'], 'running')
        with self.assertRaisesRegex(RuntimeError, 'still active'):
            self.state.recover('run', self.directory)

    def test_dead_launcher_does_not_allow_recovery_of_a_live_interpreter(self):
        tree = self.launch_fixture(independent_launcher=True)
        self.assertNotEqual(self.launcher.pid, self.actual['worker_pid'])
        self.launcher.kill()  # Deliberately reproduce the old, insufficient cleanup.
        self.launcher.wait(timeout=5)
        self.assertTrue(identity_alive(self.actual))
        with self.assertRaisesRegex(RuntimeError, 'still active'):
            self.state.recover('run', self.directory)
        row = self.state.rows('run')[0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['attempt'], 1)
        terminate_worker_tree(self.launcher, self.actual)
        self.assertFalse(identity_alive(self.actual))
        from experiments.process_control import process_identity
        self.assertIsNone(process_identity(tree['actual_child']))

    def test_mismatched_ready_identity_never_enters_processing(self):
        self.launcher = subprocess.Popen([
            sys.executable, str(self.fixture), '--worker-hello', str(self.hello),
            '--dispatch-ready', str(self.ready),
        ], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.actual = wait_worker_hello(self.hello, timeout=5)
        atomic_json(self.ready, {**self.actual, 'worker_pid': self.launcher.pid})
        self.assertNotEqual(self.launcher.wait(timeout=5), 0)
        self.assertFalse(Path(str(self.hello) + '.tree.json').exists())

    def test_helper_can_load_before_sqlite_bootstrap(self):
        probe = subprocess.run([sys.executable, '-c',
            "import sys; import experiments.process_control; assert 'sqlite3' not in sys.modules"],
            cwd=ROOT, capture_output=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
