"""Recover only a proven empty, created profile into a new short data root.
Never edits the failed source or writes seller ownership. No Accio runtime is used.
"""
from contextlib import closing, contextmanager
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid

from reception_service.storage_guard import validate_database, StorageError, REPOSITORY_VERSION, LEDGER_VERSION
from reception_service.workspace_lock import WorkspaceLock, MARKER, LOCK
from reception_service.launch_ownership import inspect_launch_ownership
from reception_service.storage_maintenance import run_maintenance

class ProfileRecoveryError(StorageError):
    def __init__(self, message):
        super().__init__(message)
        self.code = message if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', str(message)) else 'recovery_failed'

Refused = ProfileRecoveryError

class PendingRecovery(Refused):
    def __init__(self, reason, backup):
        super().__init__(reason)
        self.backup = str(backup)

def require(value, reason):
    if not value:
        raise Refused(reason)

def io_path(path):
    # Maintenance byte-copy only; never pass device paths to SQLite or routing.
    require(path.is_absolute() and not str(path).startswith(('\\\\', '//')), 'unsafe_io_path')
    return Path('\\\\?\\' + str(path)) if os.name == 'nt' else path

def reject_links(path):
    for item in (path, *path.parents):
        try:
            info = io_path(item).lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400), 'reparse_point_forbidden')

def digest(path):
    h = hashlib.sha256()
    with io_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def absolute(value):
    p = Path(value)
    require(p.is_absolute() and not str(p).startswith(('\\\\', '//')), 'local_absolute_path_required')
    p = Path(os.path.abspath(p))
    require(p != p.parent, 'volume_root_forbidden')
    reject_links(p)
    for part in p.parts[1:]:
        require(not any(c in part for c in '<>:"|?*') and part[-1:] not in ('.', ' '), 'invalid_path_component')
    return p

def related(a, b):
    return a == b or a in b.parents or b in a.parents

@contextmanager
def named_mutex(name, timeout_ms=30000):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, name)
    require(handle, 'maintenance_mutex_unavailable')
    owned = False
    try:
        owned = kernel.WaitForSingleObject(handle, timeout_ms) in (0, 0x80)
        require(owned, 'maintenance_mutex_busy')
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)

@contextmanager
def registry_mutex(registry):
    key = hashlib.sha256(str(registry).lower().encode('utf-8')).hexdigest()
    with named_mutex('Global\\ReceptionMerchantRegistry_' + key):
        yield

@contextmanager
def storage_mutex(state, timeout_ms=30000):
    key = hashlib.sha256(str(state).lower().encode('utf-8')).hexdigest()
    with named_mutex('Global\\ReceptionStorage_' + key, timeout_ms):
        yield

@contextmanager
def write_guards(paths):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    try:
        for path in paths:
            handle = kernel.CreateFileW(str(io_path(path)), 0x80000000, 1, None, 3, 0x80, None)
            require(handle != ctypes.c_void_p(-1).value, 'source_in_use')
            handles.append(handle)
        yield
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)

def files_in(directory):
    require(io_path(directory).is_dir(), 'required_directory_missing')
    found = []
    for physical in io_path(directory).iterdir():
        item = directory / physical.name
        reject_links(item)
        info = physical.lstat()
        if stat.S_ISDIR(info.st_mode):
            found.extend(files_in(item))
        else:
            require(stat.S_ISREG(info.st_mode), 'non_regular_file')
            found.append(item)
    return sorted(found)

def inventory(root):
    for item in root.iterdir():
        reject_links(item)
    require({p.name for p in root.iterdir()} <= {'host', 'live', MARKER, LOCK}, 'unexpected_root_entry')
    live = root / 'live'
    require({p.name for p in live.iterdir()} <= {'control.sqlite3', 'adapters-live.sqlite3', 'sellers', 'workctl-state'}, 'unexpected_live_entry_or_sidecar')
    files = files_in(live)
    marker = root / MARKER
    if marker.exists():
        reject_links(marker)
        require(marker.is_file(), 'invalid_marker')
        files.append(marker)
    return sorted(files)

def atomic_bytes(path, data):
    temporary = path.with_name('.recovery-' + uuid.uuid4().hex + '.tmp')
    try:
        with io_path(temporary).open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(io_path(temporary), io_path(path))
    finally:
        io_path(temporary).unlink(missing_ok=True)

def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

def qualification(root, seller_hash):
    workspace = root / 'live/sellers' / seller_hash / 'workspace.sqlite3'
    require(len((str(workspace) + '-journal').encode('utf-16le')) // 2 + 1 > 260, 'source_path_not_legacy_too_long')
    require({p.name for p in io_path(root / 'live/sellers').iterdir()} == {seller_hash}, 'unexpected_seller_directory')
    seller_dir = root / 'live/sellers' / seller_hash
    require({p.name for p in io_path(seller_dir).iterdir()} == {'workspace.sqlite3'}, 'seller_sidecar_or_unknown_file')
    require(io_path(seller_dir / 'workspace.sqlite3').is_file() and io_path(seller_dir / 'workspace.sqlite3').stat().st_size == 0, 'workspace_not_zero_bytes')
    receipts = root / 'live/workctl-state/chat-analysis/send-receipts'
    require(receipts.is_dir() and not any(receipts.iterdir()), 'receipt_history_or_missing_directory')
    native = root / 'live/workctl-state'
    require({p.name for p in native.iterdir()} <= {'cache', 'logs', 'chat-analysis'}, 'unknown_native_state')
    require({p.name for p in (native / 'chat-analysis').iterdir()} == {'send-receipts'}, 'unknown_analysis_state')
    with tempfile.TemporaryDirectory(prefix='rw-profile-check-') as tmp:
        for filename, kind in [('control.sqlite3', 'repository'), ('adapters-live.sqlite3', 'ledger')]:
            copied = Path(tmp) / filename
            shutil.copyfile(root / 'live' / filename, copied)
            require(validate_database(copied, kind) == (REPOSITORY_VERSION if kind == 'repository' else LEDGER_VERSION), 'incompatible_schema')
            with closing(sqlite3.connect(copied.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                if kind == 'repository':
                    require(db.execute("SELECT count(*) FROM entities WHERE kind IS NULL OR kind<>'meta'").fetchone()[0] == 0, 'control_has_business_entities')
                    require(db.execute("SELECT count(*) FROM revisions WHERE kind IS NULL OR kind<>'meta'").fetchone()[0] == 0, 'control_has_business_history')
                    require(db.execute("SELECT json_extract(data,'$.seller_id') FROM entities WHERE kind='meta' AND id='seller'").fetchall() == [('',)], 'control_already_bound')
                    require({row[0] for row in db.execute("SELECT id FROM entities WHERE kind='meta'")} <= {'seller', 'settings'}, 'unknown_control_metadata')
                else:
                    for table in ('operations', 'attempt_history', 'retry_grants'):
                        require(db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0, 'ledger_has_history')

def _recover(registry, seller, target, backup_parent, *, program_directory=None, fail_after=None):
    require(os.name == 'nt', 'windows_required')
    require(re.fullmatch(r'[1-9][0-9]{0,127}', seller), 'invalid_seller')
    registry, target, backup_parent = map(absolute, (registry, target, backup_parent))
    key = hashlib.sha256(seller.encode('utf-8')).hexdigest()
    longest = str(target / 'live/sellers' / key / 'workspace.sqlite3') + '-journal'
    require(len(longest.encode('utf-16le')) // 2 + 1 <= 240, 'target_path_too_long')
    require(not related(target, registry) and not related(target, backup_parent), 'target_overlaps_registry_or_backup')
    require(not related(target, Path(sys.executable).parent), 'target_overlaps_runtime')
    if program_directory is not None:
        program = absolute(program_directory)
        require(not related(target, program) and not related(backup_parent, program), 'path_overlaps_program')
    require(not related(backup_parent, registry) and not related(backup_parent, Path(sys.executable).parent), 'backup_overlaps_registry_or_runtime')
    installed = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Programs/inquiry-reception-workbench'
    require(not related(backup_parent, installed), 'backup_overlaps_installed_program')
    with registry_mutex(registry), storage_mutex(registry / 'profiles' / key / 'state'):
        registry_file = registry / 'merchants.json'
        reject_links(registry_file)
        require(registry_file.is_file() and registry_file.stat().st_size <= 1024 * 1024, 'invalid_registry_file')
        registry_bytes = registry_file.read_bytes()
        data = json.loads(registry_bytes)
        require(data.get('format') == 1 and isinstance(data.get('profiles'), dict), 'invalid_registry')
        record = data['profiles'].get(seller)
        require(record and record.get('sellerId') == seller and record.get('status') == 'created', 'not_created_profile')
        state = absolute(record['stateDirectory'])
        require(state == registry / 'profiles' / key / 'state', 'state_directory_mismatch')
        storage_file, intent_file = state / 'storage.json', state / 'profile.json'
        for path in (registry_file, storage_file, intent_file):
            reject_links(path)
            require(path.is_file() and path.stat().st_size <= 1024 * 1024, 'invalid_metadata_file')
        old_storage, old_intent = storage_file.read_bytes(), intent_file.read_bytes()
        storage, intent = json.loads(old_storage), json.loads(old_intent)
        require(isinstance(storage, dict) and storage.get('version') == 1, 'invalid_storage_metadata')
        root = absolute(storage['dataRoot'])
        require(root == absolute(record['initialDataRoot']) and 'previousRoot' not in storage, 'not_original_failed_initialization')
        require(intent == {'format':1, 'sellerId':seller, 'dataRoot':str(root), 'stateDirectory':str(state)}, 'intent_mismatch')
        require(not related(target, root) and not related(root, backup_parent), 'overlapping_source_target_backup')
        require(not target.exists(), 'target_already_exists')
        for other_seller, other in data['profiles'].items():
            require(re.fullmatch(r'[1-9][0-9]{0,127}', other_seller) and other.get('sellerId') == other_seller, 'invalid_registered_seller')
            other_state = absolute(other['stateDirectory'])
            other_key = hashlib.sha256(other_seller.encode('utf-8')).hexdigest()
            require(other_state == registry / 'profiles' / other_key / 'state', 'other_state_directory_mismatch')
            other_file = other_state / 'storage.json'
            reject_links(other_file)
            require(other_file.is_file() and other_file.stat().st_size <= 1024 * 1024, 'invalid_other_metadata')
            other_storage = json.loads(other_file.read_text(encoding='utf-8'))
            require(not related(target, other_state), 'target_conflicts_with_registered_state')
            require(not related(backup_parent, other_state), 'backup_conflicts_with_registered_state')
            if other_seller != seller:
                require(not related(root, other_state), 'source_conflicts_with_registered_state')
            for value in (other.get('initialDataRoot'), other_storage.get('dataRoot'), other_storage.get('previousRoot')):
                if value:
                    if other_seller != seller:
                        require(not related(root, absolute(value)), 'source_conflicts_with_registered_root')
                    require(not related(target, absolute(value)), 'target_conflicts_with_registered_root')
                    require(not related(backup_parent, absolute(value)), 'backup_conflicts_with_registered_root')
        lock = WorkspaceLock(root).acquire(invalidate_clean=False)
        try:
            source_files = inventory(root)
            protected = [*source_files, registry_file, storage_file, intent_file]
            with write_guards(protected):
                qualification(root, key)
                before = {str(path.relative_to(root)):digest(path) for path in source_files}
                require(inventory(root) == source_files, 'source_file_set_changed')
                result = {'status':'eligible', 'source':str(root), 'target':str(target), 'seller_suffix':seller[-4:], 'owner_written':False, 'old_source_preserved':True}
                backup_parent.mkdir(parents=True, exist_ok=True)
                backup = backup_parent / ('accio2-created-' + uuid.uuid4().hex[:12])
                backup.mkdir()
                for path in source_files:
                    relative = path.relative_to(root)
                    destination = backup / 'source' / relative
                    io_path(destination.parent).mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(io_path(path), io_path(destination))
                    with io_path(destination).open('r+b') as stream:
                        os.fsync(stream.fileno())
                    require(digest(destination) == before[str(relative)], 'backup_hash_mismatch')
                metadata_dir = backup / 'metadata'
                metadata_dir.mkdir()
                old_bytes = {registry_file:registry_bytes, storage_file:old_storage, intent_file:old_intent}
                for label, path in [('merchants.json',registry_file),('storage.json',storage_file),('profile.json',intent_file)]:
                    atomic_bytes(metadata_dir / label, old_bytes[path])
                require(inventory(root) == source_files and all(digest(root/name) == h for name,h in before.items()), 'source_changed_during_backup')
                require(all(path.read_bytes() == value for path,value in old_bytes.items()), 'metadata_changed_before_commit')
                journal_path = backup / 'recovery-transaction.json'
                journal = {'status':'prepared','seller':seller,'source':str(root),'target':str(target),'original_initial_root':record['initialDataRoot'],'source_sha256':before,'metadata_paths':{label:str(path) for label,path in [('merchants.json',registry_file),('storage.json',storage_file),('profile.json',intent_file)]}}
                atomic_bytes(journal_path, json_bytes(journal))
            # Registry/workspace locks remain held. Persistent metadata write guards
            # must be released before atomic replacement; CAS below detects changes.
            database_guards = source_files
            with write_guards(database_guards):
                require(inventory(root) == source_files and all(digest(root/name) == h for name,h in before.items()), 'source_changed_before_staging')
                target.parent.mkdir(parents=True, exist_ok=True)
                reject_links(target.parent)
                target.mkdir(exist_ok=False)
                for name in ('control.sqlite3','adapters-live.sqlite3'):
                    destination = target / 'live' / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(backup / 'source/live' / name, destination)
                    with destination.open('r+b') as stream:
                        os.fsync(stream.fileno())
                (target / 'live/workctl-state/chat-analysis/send-receipts').mkdir(parents=True)
                require(inspect_launch_ownership(target) == {'status':'empty'}, 'target_not_unbound_empty')
                require(run_maintenance('inspect', str(target))['status'] == 'completed', 'target_preflight_failed')
                new_storage = dict(storage, dataRoot=str(target), previousRoot=str(root))
                new_intent = dict(intent, dataRoot=str(target))
                new_data = json.loads(registry_bytes)
                new_data['profiles'][seller]['initialDataRoot'] = str(target)
                planned = [(intent_file,json_bytes(new_intent)),(storage_file,json_bytes(new_storage)),(registry_file,json_bytes(new_data))]
                journal['metadata_sha256'] = {path.name:hashlib.sha256(value).hexdigest() for path,value in old_bytes.items()}
                journal['planned_sha256'] = {path.name:hashlib.sha256(value).hexdigest() for path,value in planned}
                for path,value in planned:
                    atomic_bytes(metadata_dir / ('expected-' + path.name), value)
                atomic_bytes(journal_path,json_bytes(journal))
                changed = []
                target_lock = WorkspaceLock(target).acquire(invalidate_clean=False)
                staged_hashes = {name: digest(target / 'live' / name) for name in ('control.sqlite3', 'adapters-live.sqlite3')}
                journal['target_sha256'] = staged_hashes
                atomic_bytes(journal_path, json_bytes(journal))
                try:
                    for path,value in planned:
                        require(path.read_bytes() == old_bytes[path], 'metadata_compare_and_swap_failed')
                        atomic_bytes(path,value)
                        changed.append((path,value))
                        journal['status']='committing'
                        journal['committed_files']=[str(p) for p,_ in changed]
                        atomic_bytes(journal_path,json_bytes(journal))
                        if fail_after == len(changed):
                            raise Refused('synthetic_commit_failure')
                    require(inventory(root) == source_files and all(digest(root/name) == h for name,h in before.items()), 'source_changed_at_commit')
                    require(inspect_launch_ownership(target) == {'status':'empty'}, 'target_changed_at_commit')
                    require(all(path.read_bytes() == value for path,value in planned), 'metadata_final_check_failed')
                    journal['status']='completed'
                    atomic_bytes(journal_path,json_bytes(journal))
                except BaseException:
                    try:
                        safe_to_rollback = (inspect_launch_ownership(target) == {'status':'empty'} and
                                            all(digest(target / 'live' / name) == value for name, value in staged_hashes.items()))
                    except Exception:
                        safe_to_rollback = False
                    if not safe_to_rollback:
                        journal['status'] = 'rollback_requires_review'
                        try:
                            atomic_bytes(journal_path, json_bytes(journal))
                        except Exception:
                            raise PendingRecovery('recovery_log_write_failed_use_raw_metadata_backup', backup) from None
                        raise PendingRecovery('target_has_been_activated_or_changed', backup) from None
                    rollback_errors = []
                    for path,value in reversed(changed):
                        try:
                            require(path.read_bytes() == value, 'rollback_compare_and_swap_failed')
                            atomic_bytes(path,old_bytes[path])
                        except BaseException as error:
                            rollback_errors.append({'file':path.name, 'error_type':type(error).__name__})
                    journal['status']='rolled_back' if not rollback_errors else 'rollback_requires_review'
                    journal['rollback_errors']=rollback_errors
                    try:
                        atomic_bytes(journal_path,json_bytes(journal))
                    except BaseException:
                        raise PendingRecovery('recovery_log_write_failed_use_raw_metadata_backup', backup) from None
                    if rollback_errors:
                        raise PendingRecovery('metadata_rollback_incomplete_use_restore_journal', backup) from None
                    raise
                finally:
                    target_lock.release()
                return {'status':'completed', 'action':'recover-profile', 'data_root':str(target), 'backup':str(backup), 'source':str(root), 'owner_written':False}
        finally:
            lock.release()

def restore_journal(journal_path):
    journal_path = absolute(journal_path)
    require(journal_path.name == 'recovery-transaction.json' and journal_path.is_file() and journal_path.stat().st_size < 10 * 1024 * 1024, 'invalid_recovery_journal')
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    require(journal.get('status') in {'prepared','committing','rollback_requires_review','restoring_metadata','rolled_back'}, 'journal_not_pending')
    seller = journal['seller']
    require(re.fullmatch(r'[1-9][0-9]{0,127}', seller), 'invalid_journal_seller')
    key = hashlib.sha256(seller.encode()).hexdigest()
    paths = {label:absolute(value) for label,value in journal['metadata_paths'].items()}
    require(set(paths) == {'merchants.json','storage.json','profile.json'}, 'invalid_metadata_paths')
    registry = paths['merchants.json'].parent
    state = registry / 'profiles' / key / 'state'
    require(paths['storage.json'] == state/'storage.json' and paths['profile.json'] == state/'profile.json', 'journal_paths_outside_profile')
    metadata = journal_path.parent/'metadata'
    old, planned = {}, {}
    for label,path in paths.items():
        for file in (metadata/label, metadata/('expected-'+label), path):
            reject_links(file)
            require(file.is_file() and file.stat().st_size <= 1024 * 1024, 'invalid_restore_metadata')
        old[path] = (metadata/label).read_bytes()
        planned[path] = (metadata/('expected-'+label)).read_bytes()
        require(hashlib.sha256(old[path]).hexdigest() == journal['metadata_sha256'][label], 'original_metadata_hash_mismatch')
        require(hashlib.sha256(planned[path]).hexdigest() == journal['planned_sha256'][label], 'planned_metadata_hash_mismatch')
    source, target = absolute(journal['source']), absolute(journal['target'])
    old_registry = json.loads(old[paths['merchants.json']])
    require(old_registry['profiles'][seller]['status'] == 'created' and absolute(old_registry['profiles'][seller]['initialDataRoot']) == source, 'journal_original_profile_mismatch')
    require(absolute(json.loads(old[paths['storage.json']])['dataRoot']) == source, 'journal_original_root_mismatch')
    require(absolute(json.loads(planned[paths['storage.json']])['dataRoot']) == target, 'journal_planned_root_mismatch')
    with registry_mutex(registry), storage_mutex(state):
        for path in old:
            require(path.read_bytes() in (old[path], planned[path]), 'metadata_changed_since_recovery')
        if all(path.read_bytes() == old[path] for path in old):
            journal['status'] = 'restored_from_journal'
            atomic_bytes(journal_path,json_bytes(journal))
            return {'status':'restored_metadata','source':str(source),'target_retained':str(target),'business_databases_modified':False}
        source_lock = WorkspaceLock(source).acquire(invalidate_clean=False)
        target_lock = None
        try:
            require(target.is_dir(), 'staged_target_missing')
            target_lock = WorkspaceLock(target).acquire(invalidate_clean=False)
            require(inspect_launch_ownership(target) == {'status':'empty'}, 'target_has_been_activated_or_changed')
            source_files = inventory(source)
            require({str(p.relative_to(source)) for p in source_files} == set(journal['source_sha256']), 'original_source_file_set_changed')
            target_files = [target/'live/control.sqlite3',target/'live/adapters-live.sqlite3']
            target_hashes = journal.get('target_sha256')
            require(isinstance(target_hashes, dict) and set(target_hashes) == {'control.sqlite3', 'adapters-live.sqlite3'}, 'missing_staged_target_hashes')
            with write_guards([*source_files,*target_files]):
                require(all(digest(target/'live'/name) == value for name, value in target_hashes.items()), 'target_has_been_activated_or_changed')
                require(all(digest(source/name) == value for name,value in journal['source_sha256'].items()), 'original_source_changed')
                require(inspect_launch_ownership(target) == {'status':'empty'}, 'target_changed_before_restore')
                journal['status']='restoring_metadata'
                atomic_bytes(journal_path,json_bytes(journal))
                for path in (paths['profile.json'],paths['storage.json'],paths['merchants.json']):
                    current=path.read_bytes()
                    require(current in (old[path],planned[path]), 'restore_compare_and_swap_failed')
                    if current != old[path]:
                        atomic_bytes(path,old[path])
                require(all(path.read_bytes() == value for path,value in old.items()), 'metadata_restore_incomplete')
                journal['status']='restored_from_journal'
                atomic_bytes(journal_path,json_bytes(journal))
                return {'status':'restored_metadata','source':str(source),'target_retained':str(target),'business_databases_modified':False}
        finally:
            if target_lock is not None:
                target_lock.release()
            source_lock.release()


def recover_created_profile(registry_directory, seller_id, target, backup_parent, *, program_directory=None):
    """Recover a proven failed first initialization; never create seller ownership."""
    try:
        return _recover(registry_directory, seller_id, target, backup_parent, program_directory=program_directory)
    except ProfileRecoveryError:
        raise
    except Exception as exc:
        raise ProfileRecoveryError('recovery_failed') from exc
