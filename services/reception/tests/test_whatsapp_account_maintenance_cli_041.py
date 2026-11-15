import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reception_service.whatsapp_account_service import AccountRepository


class MaintenanceCliTests(unittest.TestCase):
    def run_cli(self, *args):
        result = subprocess.run([sys.executable, '-B', '-m', 'reception_service', *map(str, args)],
                                capture_output=True, text=True, encoding='utf-8', env=os.environ.copy(), timeout=30)
        self.assertEqual(result.stderr, '')
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        return result.returncode, json.loads(lines[0])

    def test_roundtrip_and_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'root'
            repo = AccountRepository(root, 'workspace')
            repo.put('operation', 'op', {'status': 'unknown'})
            code, result = self.run_cli('--whatsapp-maintenance', 'backup', '--source', root, '--destination', base / 'busy')
            self.assertEqual((code, result['status']), (1, 'refused'))
            repo.close()
            archive = base / 'archive'
            self.assertEqual(self.run_cli('--whatsapp-maintenance', 'backup', '--source', root, '--destination', archive)[0], 0)
            target = base / 'restored'
            code, result = self.run_cli('--whatsapp-maintenance', 'restore', '--archive', archive, '--destination', target)
            self.assertEqual(code, 0)
            self.assertFalse(result['reception_enabled'])
            restored = AccountRepository(target, 'workspace')
            self.assertEqual(restored.get('operation', 'op')['status'], 'unknown')
            restored.close()
            code, result = self.run_cli('--whatsapp-maintenance', 'restore', '--archive', archive, '--destination', target)
            self.assertEqual((code, result['status']), (1, 'refused'))
            self.assertNotIn(str(base), json.dumps(result))

    def test_strict_selection(self):
        for extra in [[], ['--source', 'x'], ['--source', 'x', '--destination', 'y', '--archive', 'z'],
                      ['--source', 'x', '--destination', 'y', '--whatsapp-account'],
                      ['--source', 'x', '--destination', 'y', '--maintenance', 'backup'],
                      ['--source', 'x', '--destination', 'y', '--data-dir', 'z'],
                      ['--source', 'x', '--source', 'y', '--destination', 'z'],
                      ['--source', 'x', '--destination', 'y', '--dest', 'z']]:
            with self.subTest(extra=extra):
                code, result = self.run_cli('--whatsapp-maintenance', 'backup', *extra)
                self.assertEqual((code, result['code']), (2, 'invalid_arguments'))


if __name__ == '__main__':
    unittest.main()
