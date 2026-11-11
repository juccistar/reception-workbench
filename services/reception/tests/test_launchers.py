"""Exercise real cmd.exe with synthetic marker code, never the business app."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]

@unittest.skipUnless(os.name == 'nt' and shutil.which('node'), 'Windows and Node are required')
class LauncherTests(unittest.TestCase):
    def check_launcher(self, name, expected):
        with tempfile.TemporaryDirectory(prefix='reception-launcher-') as temporary:
            base = Path(temporary)
            project = base / '中文 测试工程'
            scripts = project / 'scripts'
            scripts.mkdir(parents=True)
            (base / 'other').mkdir()
            for source, destination in ((ROOT / name, project / name), (ROOT / 'scripts/launch.cmd', scripts / 'launch.cmd')):
                data = source.read_bytes()
                self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
                self.assertTrue(data.isascii())
                destination.write_bytes(data)
            (scripts / 'start.mjs').write_text('import { writeFileSync } from "node:fs"; writeFileSync(process.argv[2],JSON.stringify(process.argv.slice(3)));', encoding='utf-8')
            marker = base / 'marker.json'
            # The actual helper passes --connect-from-accio before the marker.
            if expected:
                (scripts / 'start.mjs').write_text('import { writeFileSync } from "node:fs"; writeFileSync(process.argv[3],JSON.stringify([process.argv[2],...process.argv.slice(4)]));', encoding='utf-8')
            comspec = os.environ.get('COMSPEC', 'cmd.exe')
            # cmd.exe has its own quote grammar; CRT list2cmdline escapes nested
            # quotes with backslashes, which cmd treats as literal path bytes.
            command = f'"{comspec}" /d /s /c ""{project / name}" "{marker}" --synthetic-marker"'
            result = subprocess.run(command, executable=comspec, cwd=base / 'other', input=b'\n', stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('utf-8', errors='replace'))
            self.assertTrue(marker.is_file())
            self.assertEqual(json.loads(marker.read_text(encoding='utf-8')), [*expected, '--synthetic-marker'])

    def test_demo_launcher_from_unrelated_directory(self):
        self.check_launcher('启动工作台.cmd', [])

    def test_connection_launcher_preserves_authorized_start_flag(self):
        self.check_launcher('连接Accio Work.cmd', ['--connect-from-accio'])
