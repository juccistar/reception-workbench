import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'services/reception/src'
SUPPORT = ROOT / 'services/reception/test_support'
ENV = {**os.environ, 'PYTHONPATH': str(SRC), 'PYTHONIOENCODING': 'utf-8'}


class ProductionEntryTests(unittest.TestCase):
    def test_source_closure(self):
        spec = importlib.util.spec_from_file_location('closure', ROOT / 'scripts/verify-production-closure.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            staged = Path(temp) / 'reception_service'
            shutil.copytree(SRC / 'reception_service', staged,
                            ignore=shutil.ignore_patterns('demo.py', '__pycache__', '*.pyc'))
            module.verify_source(staged)

    def test_cli_rejects_demo(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'reception_service', '--mode', 'demo'],
                                env=ENV, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('invalid choice', result.stderr)

    def test_synthetic_jsonl_start_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, '-B', str(SUPPORT / 'synthetic_service.py'), '--data-dir', temp],
                                    input='{"id":1,"method":"get_snapshot"}\n{"id":2,"method":"shutdown"}\n',
                                    env=ENV, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            messages = [json.loads(line) for line in result.stdout.splitlines()]
            responses = {m['id']: m for m in messages if 'id' in m}
            self.assertEqual(len(responses[1]['result']['conversations']), 3)
            self.assertIn('result', responses[2])

    def test_maintenance_cancel_and_progress(self):
        code = '''
import sys, time
from reception_service import __main__ as cli
from reception_service import storage_maintenance as storage
from reception_service.storage_guard import StorageError
def run(*args, progress, cancel, **kwargs):
    progress('validated')
    deadline = time.monotonic() + 5
    while not cancel() and time.monotonic() < deadline:
        time.sleep(.01)
    if cancel():
        raise StorageError('cancelled', code='storage_cancelled')
    raise RuntimeError('cancel not received')
storage.run_maintenance = run
sys.argv = ['backend', '--maintenance', 'inspect']
cli.main()
'''
        proc = subprocess.Popen([sys.executable, '-B', '-c', code], env=ENV, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        out, err = proc.communicate('{"command":"cancel"}\n', timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(out)['code'], 'storage_cancelled')
        self.assertEqual(json.loads(err)['stage'], 'validated')


if __name__ == '__main__':
    unittest.main()
