"""Fail packaging if synthetic code remains in source or the frozen archive."""
import ast
from pathlib import Path
import sys


def verify_source(root):
    root = Path(root)
    forbidden = {'_seed', 'demo_source_product', 'demo_reconcile_source_product'}
    for file in root.rglob('*.py'):
        if file.name == 'demo.py' or 'test_support' in file.parts:
            raise RuntimeError('Synthetic module in production staging: ' + str(file))
        tree = ast.parse(file.read_text(encoding='utf-8'), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
                raise RuntimeError('Synthetic fixture in production staging: ' + str(file) + ':' + node.name)
            if isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if module == 'demo' or module.endswith('.demo') or 'test_support' in module:
                    raise RuntimeError('Synthetic import in production staging: ' + str(file) + ':' + module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith('.demo') or 'test_support' in alias.name:
                        raise RuntimeError('Synthetic import in production staging: ' + str(file) + ':' + alias.name)


def verify_frozen(executable):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    names = list(archive.toc)
    pyz = archive.open_embedded_archive('PYZ.pyz')
    names.extend(pyz.toc)
    banned = ('reception_service.integrations.demo', 'reception_test_support', 'synthetic_harness')
    for name in names:
        canonical = name.replace('\\', '/').replace('/', '.')
        if any(canonical == item or canonical.startswith(item + '.') for item in banned):
            raise RuntimeError('Synthetic module in frozen archive: ' + name)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify-production-closure.py staged-source executable')
    verify_source(sys.argv[1])
    verify_frozen(sys.argv[2])
