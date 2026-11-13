"""Offline managed-workspace backup and restore; never copies credentials."""
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import tempfile

from .storage_guard import StorageError, preflight_workspace
from .workspace_lock import WorkspaceLock, reject_links

RECEIPTS = 'live/workctl-state/chat-analysis/send-receipts'
MANIFEST = 'manifest.json'
PROVENANCE = '.receipt-roots.json'


def _long_path(path):
    """Use extended paths for deep hash-named ledgers on Windows."""
    value = os.path.abspath(path)
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = ('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return Path(value)


def _normal_path(path):
    value = str(path)
    if os.name == 'nt' and value.startswith('\\\\?\\UNC\\'):
        return Path('\\\\' + value[8:])
    if os.name == 'nt' and value.startswith('\\\\?\\'):
        return Path(value[4:])
    return Path(value)


def _absolute_parts(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise StorageError('Invalid receipt root')
    windows = PureWindowsPath(value)
    if windows.is_absolute():
        # Reject ambiguous, traversal and alternate data stream paths.
        text = value.replace('\\', '/')
        if text.startswith('//'):
            components = text[2:].split('/')
            if len(components) < 3 or components[0] in ('?', '.'):
                raise StorageError('Invalid receipt path')
            parts = ['//', *components]
            start = 1
        else:
            parts = text.split('/')
            if not (len(parts[0]) == 2 and parts[0][1] == ':' and parts[0][0].isalpha()):
                raise StorageError('Invalid receipt path')
            start = 1
        if any(p in ('', '.', '..') or ':' in p for p in parts[start:]):
            raise StorageError('Invalid receipt path')
        return 'windows', tuple(p.casefold() for p in parts), tuple(parts)
    if value.startswith('/'):
        parts = value.split('/')
        if any(p in ('', '.', '..') for p in parts[1:]) or '\\' in value or ':' in value:
            raise StorageError('Invalid receipt path')
        return 'posix', tuple(parts), tuple(parts)
    raise StorageError('Invalid receipt root')


def _receipt_roots(root):
    path = root / PROVENANCE
    reject_links(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
        if not isinstance(data, dict) or set(data) not in ({'receipt_roots'}, {'receipt_roots', 'receipt_hashes'}) or not isinstance(data['receipt_roots'], list):
            raise StorageError('Invalid receipt provenance')
        roots = data['receipt_roots']
        hashes = data.get('receipt_hashes', {})
        if not isinstance(hashes, dict) or len(hashes) > 100000:
            raise StorageError('Invalid receipt provenance')
        for name, digest in hashes.items():
            if (not isinstance(name, str) or not name or '\\' in name or ':' in name
                    or any(part in ('', '.', '..') for part in name.split('/'))
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(char not in '0123456789abcdef' for char in digest)):
                raise StorageError('Invalid receipt provenance')
            receipt = root / RECEIPTS / name
            reject_links(receipt)
            if not receipt.is_file() or _hash(receipt) != digest:
                raise StorageError('Receipt provenance failed verification')
        for item in roots:
            _, _, parts = _absolute_parts(item)
            if tuple(p.casefold() for p in parts[-4:]) != tuple(RECEIPTS.split('/')):
                raise StorageError('Invalid receipt provenance')
        if len(roots) != len(set(roots)):
            raise StorageError('Invalid receipt provenance')
        return roots
    except (OSError, ValueError, TypeError) as exc:
        raise StorageError('Invalid receipt provenance') from exc


def _write_provenance(stage, roots, entries):
    roots = sorted(set(roots))
    path = stage / PROVENANCE
    hashes = {entry['path'][len(RECEIPTS) + 1:]: entry['sha256'] for entry in entries
              if entry['path'].startswith(RECEIPTS + '/')}
    path.write_text(json.dumps({'receipt_roots': roots, 'receipt_hashes': hashes}, sort_keys=True), encoding='utf-8')
    stat = path.stat()
    entries.append({'path': PROVENANCE, 'size': stat.st_size,
                    'sha256': _hash(path), 'mtime_ns': stat.st_mtime_ns})

def _hash(path):
    h = hashlib.sha256()
    with _long_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def _files(root, *, require_checkpoint=True, check_references=True, receipt_roots=()):
    root = _long_path(root)
    files = []
    def add(path):
        reject_links(path)
        if not path.is_file():
            raise StorageError('Required file missing')
        if path.suffix == '.sqlite3':
            wal = Path(str(path) + '-wal')
            reject_links(wal)
            if require_checkpoint and wal.exists() and wal.stat().st_size:
                raise StorageError('Backup requires databases checkpointed at clean shutdown')
        files.append(path.relative_to(root).as_posix())
    def add_whatsapp_ledgers(base):
        directory = base / 'whatsapp-ledgers'
        reject_links(directory)
        if not directory.exists():
            return
        if not directory.is_dir():
            raise StorageError('Invalid WhatsApp ledger directory')
        for seller in directory.iterdir():
            reject_links(seller)
            if not seller.is_dir() or not re.fullmatch(r'[0-9a-f]{64}', seller.name):
                raise StorageError('Invalid WhatsApp ledger seller')
            children = list(seller.iterdir())
            if len(children) != 1 or children[0].name != 'send-ledger':
                raise StorageError('Invalid WhatsApp ledger layout')
            ledger = children[0]
            reject_links(ledger)
            if not ledger.is_dir():
                raise StorageError('Invalid WhatsApp ledger directory')
            for path in ledger.iterdir():
                reject_links(path)
                if not re.fullmatch(r'[0-9a-f]{64}\.jsonl', path.name) or not path.is_file():
                    raise StorageError('Invalid WhatsApp ledger entry')
                add(path)

    for mode in ('demo', 'live'):
        base = root / mode
        reject_links(base)
        if not base.exists():
            continue
        add_whatsapp_ledgers(base)
        add(base / 'control.sqlite3')
        if mode == 'demo':
            add(base / 'workspace.sqlite3')
            for ledger_name in ('integrations-demo.sqlite3', 'adapters-live.sqlite3'):
                demo_ledger = base / ledger_name
                reject_links(demo_ledger)
                if demo_ledger.exists():
                    add(demo_ledger)
        else:
            add(base / 'adapters-live.sqlite3')
            sellers = base / 'sellers'
            reject_links(sellers)
            if sellers.exists():
                for seller in sellers.iterdir():
                    reject_links(seller)
                    if not seller.is_dir():
                        raise StorageError('Invalid seller directory')
                    add(seller / 'workspace.sqlite3')
            receipts = root / RECEIPTS
            reject_links(receipts)
            if not receipts.is_dir():
                raise StorageError('Receipt directory missing')
            for directory, dirs, names in os.walk(receipts, followlinks=False):
                for name in dirs:
                    reject_links(Path(directory) / name)
                for name in names:
                    add(Path(directory) / name)
    if not files:
        raise StorageError('No workspace databases')
    provenance = root / PROVENANCE
    reject_links(provenance)
    if provenance.exists():
        add(provenance)
    if check_references:
        _check_receipt_references(root, files, [*_receipt_roots(root), *receipt_roots])
    return sorted(files)

def _check_receipt_references(root, files, receipt_roots=()):
    """Require receipts referenced by durable evidence and baseline inventories."""
    references = set()
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            ref = value.get('native_receipt_ref')
            if ref:
                if not isinstance(ref, str):
                    raise StorageError('Invalid receipt reference')
                references.add(ref)
            baseline = value.get('receipt_baseline')
            if isinstance(baseline, dict):
                references.update(baseline)
            for item in value.values():
                visit(item)
    for name in files:
        if not name.startswith('live/') or not name.endswith('.sqlite3'):
            continue
        db = sqlite3.connect(_normal_path(root / name).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            queries = ('SELECT request FROM operations', 'SELECT result FROM operations') if name.endswith('adapters-live.sqlite3') else ('SELECT data FROM entities', 'SELECT data FROM revisions')
            for query in queries:
                for (text,) in db.execute(query):
                    if text is not None:
                        try:
                            visit(json.loads(text))
                        except (ValueError, TypeError) as exc:
                            raise StorageError('Invalid stored JSON') from exc
        finally:
            db.close()
    roots = [_absolute_parts(item) for item in receipt_roots]
    for ref in references:
        if not isinstance(ref, str) or not ref or '\x00' in ref:
            raise StorageError('Invalid receipt reference')
        if PureWindowsPath(ref).is_absolute() or ref.startswith('/'):
            kind, normalized, original = _absolute_parts(ref)
            matches = [original[len(parts):] for root_kind, parts, _ in roots
                       if kind == root_kind and normalized[:len(parts)] == parts
                       and len(normalized) > len(parts)]
            if not matches:
                raise StorageError('Invalid receipt reference')
            relative = '/'.join(matches[0])
        else:
            relative = ref.replace('\\', '/')
            parts = relative.split('/')
            if any(p in ('', '.', '..') or ':' in p for p in parts):
                raise StorageError('Invalid receipt reference')
        if RECEIPTS + '/' + relative not in files:
            raise StorageError('Referenced receipt missing')

def _target(path):
    path = Path(os.path.abspath(path))
    reject_links(path)
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise StorageError('Target must be absent or empty')
    reject_links(path.parent)
    if not path.parent.is_dir():
        raise StorageError('Target parent missing')
    return path

def _publish(stage, target):
    if target.exists():
        target.rmdir()
    os.rename(stage, target)

def _separate(a, b):
    if a == b or a in b.parents or b in a.parents:
        raise StorageError('Source and target must be separate')

def backup_workspace(data_root, destination):
    root = Path(os.path.abspath(data_root))
    target = _target(destination)
    reject_links(root)
    _separate(root, target)
    if not root.is_dir():
        raise StorageError('Workspace does not exist')
    with WorkspaceLock(root) as lock:
        if not lock.was_clean:
            raise StorageError('Workspace has no managed clean shutdown')
        try:
            preflight_workspace(root)
            roots = [*_receipt_roots(root), str(root / RECEIPTS)]
            paths = _files(root, receipt_roots=roots)
            stage = _long_path(tempfile.mkdtemp(prefix='.reception-backup-', dir=target.parent))
            try:
                entries = []
                for name in paths:
                    if name == PROVENANCE:
                        continue
                    source, dest = _long_path(root / name), _long_path(stage / name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    s = source.stat()
                    entries.append({'path': name, 'size': s.st_size, 'sha256': _hash(dest), 'mtime_ns': s.st_mtime_ns})
                if (root / RECEIPTS).is_dir():
                    (stage / RECEIPTS).mkdir(parents=True, exist_ok=True)
                _write_provenance(stage, roots, entries)
                (stage / MANIFEST).write_text(json.dumps({'format': 1, 'files': entries}, sort_keys=True), encoding='utf-8')
                _validate_archive(stage)
                _publish(stage, target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        finally:
            lock.mark_clean_shutdown()
    return target

def _unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise StorageError('Duplicate manifest key')
        result[k] = v
    return result

def _validate_archive(root):
    root = _long_path(root)
    reject_links(root / MANIFEST)
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding='utf-8'), object_pairs_hook=_unique)
        if set(manifest) != {'format', 'files'} or manifest['format'] != 1 or not isinstance(manifest['files'], list):
            raise StorageError('Invalid manifest')
        names = set()
        for entry in manifest['files']:
            if set(entry) != {'path', 'size', 'sha256', 'mtime_ns'}:
                raise StorageError('Invalid manifest entry')
            name = entry['path']
            if not isinstance(name, str) or '\\' in name or ':' in name:
                raise StorageError('Invalid archive path')
            p = PurePosixPath(name)
            if p.is_absolute() or str(p) != name or any(x in ('.', '..') for x in p.parts) or name in names:
                raise StorageError('Unsafe or duplicate archive path')
            names.add(name)
            file = root.joinpath(*p.parts)
            reject_links(file)
            if (type(entry['size']) is not int or type(entry['mtime_ns']) is not int or entry['mtime_ns'] < 0
                or not file.is_file() or file.stat().st_size != entry['size'] or _hash(file) != entry['sha256']):
                raise StorageError('Backup file failed verification')
        actual = set(_files(root))
        if actual != names:
            raise StorageError('Backup is incomplete or contains unexpected entries')
        for directory, dirs, files in os.walk(root, followlinks=False):
            for item in dirs + files:
                path = Path(directory) / item
                reject_links(path)
                if path.is_file() and path.relative_to(root).as_posix() not in names | {MANIFEST}:
                    raise StorageError('Unexpected backup file')
        preflight_workspace(root)
        return manifest
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise StorageError('Invalid backup') from exc

def restore_workspace(archive, destination):
    source = Path(os.path.abspath(archive))
    target = _target(destination)
    reject_links(source)
    _separate(source, target)
    manifest = _validate_archive(source)
    stage = _long_path(tempfile.mkdtemp(prefix='.reception-restore-', dir=target.parent))
    try:
        for entry in manifest['files']:
            name = entry['path']
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_long_path(source / name), dest)
            os.utime(dest, ns=(entry['mtime_ns'], entry['mtime_ns']))
            if name.startswith(RECEIPTS + '/') and dest.stat().st_mtime_ns != entry['mtime_ns']:
                raise StorageError('Receipt timestamp precision could not be preserved')
            if _hash(dest) != entry['sha256']:
                raise StorageError('Restored file failed verification')
        if (source / RECEIPTS).is_dir():
            (stage / RECEIPTS).mkdir(parents=True, exist_ok=True)
        preflight_workspace(stage)
        if set(_files(stage)) != {e['path'] for e in manifest['files']}:
            raise StorageError('Restored workspace is incomplete')
        with WorkspaceLock(stage) as lock:
            lock.mark_clean_shutdown()
        _publish(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return target
