"""Read-only compatibility checks for every persistent SQLite store."""
from pathlib import Path
import shutil
import sqlite3
import tempfile

REPOSITORY_VERSION = 2
LEDGER_VERSION = 1

class StorageError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def check_sqlite_path(path):
    # SQLite creates sidecars before reporting some Windows path errors.
    if path is not None and __import__('os').name == 'nt':
        longest = str(Path(path).absolute()) + '-journal'
        if len(longest.encode('utf-16-le')) // 2 + 1 > 260:
            raise StorageError('SQLite path is too long', code='storage_path_too_long')

REPOSITORY_SCHEMA = (
    'CREATE TABLE entities (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind,id))',
    'CREATE TABLE revisions (sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, saved_at INTEGER NOT NULL)',
    'CREATE INDEX revisions_entity ON revisions(kind,id,sequence)',
    "CREATE INDEX entities_conversation ON entities(kind,json_extract(data,'$.conversation_id'))",
    "CREATE INDEX entities_case ON entities(kind,json_extract(data,'$.case_id'))",
)
LEDGER_SCHEMA = (
    'CREATE TABLE operations (operation_id TEXT PRIMARY KEY, logical_key TEXT UNIQUE, kind TEXT, request TEXT, result TEXT)',
    'CREATE TABLE retry_grants (operation_id TEXT PRIMARY KEY)',
    'CREATE TABLE attempt_history (seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, result TEXT NOT NULL, stamp INTEGER NOT NULL)',
)

def _signature(db):
    # table_info catches affinity, defaults, nullability and primary-key ordering;
    # index SQL catches expressions, uniqueness and partial predicates.
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    other = list(db.execute("SELECT type,name FROM sqlite_master WHERE type NOT IN ('table','index')"))
    if other:
        raise StorageError('Unsupported SQLite objects')
    columns = {name: list(db.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')) for name in tables}
    indexes = list(db.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"))
    def norm(s):
        return ''.join(s.lower().split())
    constraints = {}
    for name in tables:
        quoted = name.replace('"', '""')
        constraints[name] = sorted((row[2], row[3], row[4], tuple(
            part[2] for part in db.execute('PRAGMA index_info("' + row[1].replace('"', '""') + '")')))
            for row in db.execute('PRAGMA index_list("' + quoted + '")'))
    definitions = [(name, norm(sql)) for name, sql in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return columns, constraints, definitions, [(n, t, norm(s)) for n, t, s in indexes]

def _expected(schema):
    with sqlite3.connect(':memory:') as db:
        for sql in schema:
            db.execute(sql)
        return _signature(db)

def validate_database(path, kind):
    check_sqlite_path(path)
    path = Path(path)
    from .workspace_lock import reject_links
    reject_links(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise StorageError('Database is not a regular file')
    wal = Path(str(path) + '-wal')
    reject_links(wal)
    reject_links(Path(str(path) + '-shm'))
    try:
        with tempfile.TemporaryDirectory(prefix='reception-preflight-') as temp:
            # Never attach SQLite to the source. In particular it must not create,
            # delete, checkpoint or repair any source WAL or shared-memory file.
            copied = Path(temp) / 'database.sqlite3'
            shutil.copyfile(path, copied)
            if wal.exists() and wal.stat().st_size:
                if wal.stat().st_size < 32:
                    raise StorageError('Invalid SQLite WAL')
                shutil.copyfile(wal, Path(str(copied) + '-wal'))
            db = sqlite3.connect(copied)
            try:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if (kind == 'repository' and version not in (1, REPOSITORY_VERSION)) or (kind == 'ledger' and version not in (0, LEDGER_VERSION)):
                    raise StorageError('Unsupported database version')
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise StorageError('Database integrity check failed')
                if _signature(db) != _expected(REPOSITORY_SCHEMA if kind == 'repository' else LEDGER_SCHEMA):
                    raise StorageError('Unrecognized database schema')
                return version
            finally:
                db.close()
    except (sqlite3.Error, OSError) as exc:
        raise StorageError('Invalid database') from exc

def preflight_workspace(data_root, *, validated=None):
    root = Path(data_root)
    from .workspace_lock import reject_links
    reject_links(root)
    paths = [root / 'demo/control.sqlite3', root / 'demo/workspace.sqlite3', root / 'live/control.sqlite3']
    sellers = root / 'live/sellers'
    if sellers.exists():
        reject_links(sellers)
        for seller in sellers.iterdir():
            reject_links(seller)
            if not seller.is_dir():
                raise StorageError('Unexpected seller workspace entry')
            if not (seller / 'workspace.sqlite3').is_file():
                raise StorageError('Seller workspace database missing')
            paths.append(seller / 'workspace.sqlite3')
    def check(path, kind):
        key = (path, kind)
        if validated is None or key not in validated:
            version = validate_database(path, kind)
            if validated is not None:
                validated[key] = version
    for path in paths:
        check(path, 'repository')
    check(root / 'demo/integrations-demo.sqlite3', 'ledger')
    for mode in ('demo', 'live'):
        check(root / mode / 'adapters-live.sqlite3', 'ledger')
