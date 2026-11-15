#!/usr/bin/env python3
"""Synthetic repository pagination benchmark; never opens an existing business database."""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/reception/src'))
from reception_service.repository import Repository, encoded  # noqa: E402

SCENARIOS = {'small': (3, 240), '100x50000': (100, 50000), '500x200000': (500, 200000)}
PAGE_SIZE = 50
QUERY = """SELECT data FROM entities WHERE kind=? AND json_extract(data,'$.conversation_id')=?
ORDER BY COALESCE(json_extract(data,'$.timestamp_ms'),-1) DESC,
json_extract(data,'$.message_id') DESC LIMIT ?"""


def key(message):
    return (message['timestamp_ms'] if message['timestamp_ms'] is not None else -1, message['message_id'])


def populate(repo, conversations, count):
    with repo.transaction():
        rows = []
        for i in range(count):
            cid = f'conversation-{i % conversations:04d}'
            msg = {'message_id': f'message-{i:07d}', 'conversation_id': cid,
                   'timestamp_ms': None if i % 17 == 0 else (i // (conversations * 3)),
                   'case_id': f'{cid}-case-{i % 2}', 'text': f'synthetic-{i}'}
            rows.append(('message', msg['message_id'], encoded(msg)))
        repo.db.executemany('INSERT INTO entities(kind,id,data) VALUES(?,?,?)', rows)
        for c in range(conversations):
            cid = f'conversation-{c:04d}'
            for case in range(2):
                repo.put('case', f'{cid}-case-{case}', {'conversation_id': cid, 'case_id': f'{cid}-case-{case}'})
            repo.put('operation', f'{cid}-unknown', {'conversation_id': cid, 'status': 'unknown'})


def verify(repo, cid):
    old = sorted(repo.list('message', conversation_id=cid), key=key)
    pages = []
    before = None
    while True:
        page = repo.message_page(cid, PAGE_SIZE, before)
        if not page:
            break
        pages.insert(0, page)
        before = key(page[0])
    actual = [message for page in pages for message in page]
    if actual != old or repo.message_page(cid, PAGE_SIZE) != old[-PAGE_SIZE:]:
        raise AssertionError('pagination differs from list/sort/slice')
    if len(repo.list('case', conversation_id=cid)) != 2 or not any(
            op.get('status') == 'unknown' for op in repo.list('operation', conversation_id=cid)):
        raise AssertionError('synthetic case/unknown fixture missing')
    return len(old)


def process_metrics():
    result = {'pid': os.getpid(), 'working_set_bytes': None, 'private_bytes': None,
              'thread_count': None, 'handle_count': None, 'source': 'unavailable'}
    if os.name != 'nt':
        return result
    from ctypes import wintypes
    class MemoryCounters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
             'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
             'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel.GetCurrentProcess()
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        result.update(working_set_bytes=counters.WorkingSetSize, private_bytes=counters.PrivateUsage)
    handles = wintypes.DWORD()
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    if kernel.GetProcessHandleCount(process, ctypes.byref(handles)):
        result['handle_count'] = handles.value
    # Toolhelp snapshot includes native threads, not just Python threading objects.
    class ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ThreadID', wintypes.DWORD), ('th32OwnerProcessID', wintypes.DWORD),
                    ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot != ctypes.c_void_p(-1).value:
        entry = ThreadEntry(); entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Thread32First(snapshot, ctypes.byref(entry))
        count = 0
        while valid:
            count += entry.th32OwnerProcessID == os.getpid()
            valid = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        kernel.CloseHandle(snapshot)
        result['thread_count'] = count
    result['source'] = 'Windows process APIs; current Python process only'
    return result


def summary(samples, parsed):
    ordered = sorted(samples)
    return {'count': len(samples), 'median_ms': statistics.median(samples),
            'p95_ms': ordered[math.ceil(len(ordered) * .95) - 1], 'max_ms': max(samples),
            'returned_rows_per_call': min(PAGE_SIZE, parsed), 'parsed_rows_per_call': parsed}


def run_scenario(name, measure, repetitions):
    conversations, count = SCENARIOS[name]
    with tempfile.TemporaryDirectory(prefix='reception-benchmark-040-') as directory:
        path = Path(directory) / 'synthetic.sqlite3'
        repo = Repository(path, 'synthetic-benchmark-only')
        try:
            populate(repo, conversations, count)
            cid = 'conversation-0000'
            parsed = verify(repo, cid)
            plan = [list(row) for row in repo.db.execute('EXPLAIN QUERY PLAN ' + QUERY, ('message', cid, PAGE_SIZE))]
            report = {'scenario': name, 'conversation_count': conversations, 'message_count': count,
                      'case_count': conversations * 2, 'unknown_operation_count': conversations,
                      'image_count': 0, 'task_count': 0, 'target_conversation': cid,
                      'database_bytes': path.stat().st_size, 'wal_bytes': Path(str(path) + '-wal').stat().st_size if Path(str(path) + '-wal').exists() else 0,
                      'correctness': 'all pages and newest page match list/sort/slice',
                      'query_plan': plan, 'sql_scan_rows': None,
                      'sql_scan_note': 'EXPLAIN QUERY PLAN describes access and sorting, not actual scanned rows; LIMIT only bounds returned/decoded rows.',
                      'timings': None}
            if measure:
                samples = {'list_sort_slice': [], 'message_page': []}
                # Alternating execution order reduces systematic ordering bias.
                for i in range(repetitions):
                    actions = [('list_sort_slice', lambda: sorted(repo.list('message', conversation_id=cid), key=key)[-PAGE_SIZE:]),
                               ('message_page', lambda: repo.message_page(cid, PAGE_SIZE))]
                    for label, action in actions[::1 if i % 2 == 0 else -1]:
                        start = time.perf_counter_ns(); action()
                        samples[label].append((time.perf_counter_ns() - start) / 1_000_000)
                report['timings'] = {'list_sort_slice': summary(samples['list_sort_slice'], parsed),
                                     'message_page': summary(samples['message_page'], min(PAGE_SIZE, parsed))}
            return report
        finally:
            repo.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True, help='new directory name under artifacts/verification')
    parser.add_argument('--scenario', choices=[*SCENARIOS, 'all'], default='small')
    parser.add_argument('--measure', action='store_true', help='run timing; large scenarios only during quiet period')
    parser.add_argument('--repetitions', type=int, default=200)
    args = parser.parse_args()
    if not args.label or args.label in ('.', '..') or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for ch in args.label):
        parser.error('label must contain only ASCII letters, digits, hyphen or underscore')
    if args.measure and args.repetitions < 200:
        parser.error('timing requires at least 200 repetitions')
    base = (ROOT / 'artifacts' / 'verification').resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = base / args.label
    destination.mkdir(exist_ok=False)
    if not args.measure:
        tracemalloc.start()
    before = process_metrics()
    scenarios = list(SCENARIOS) if args.scenario == 'all' else [args.scenario]
    results = [run_scenario(name, args.measure, args.repetitions) for name in scenarios]
    current, peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (None, None)
    report = {'status': 'measured' if args.measure else 'correctness_only_timing_pending',
              'environment': {'platform': platform.platform(), 'python': sys.version, 'processor': platform.processor(),
                              'os_cache': 'uncontrolled; no cold-cache claim'},
              'scenarios': results, 'process_before': before, 'process_after': process_metrics(),
              'tracemalloc_bytes': {'current': current, 'peak': peak, 'note': 'Python allocations only; supplementary'},
              'startup_samples': {'source': {'first_run': [], 'restart': []}, 'candidate': {'first_run': [], 'restart': []},
                                  'sample_fields': ['elapsed_ms', 'stage', 'process_metrics', 'exit_status', 'stdout_bytes', 'stderr_bytes'],
                                  'status': 'not measured'},
              'pending_quiet_period_command': 'python scripts/benchmark-040.py --label benchmark-040-quiet --scenario all --measure',
              'limitations': ['Synthetic repository only; no app startup, image load, real customers, system capacity or leak assessment.',
                              'Process metrics are snapshots of this Python process, not a process tree or peak measurement.',
                              'SQL scan counts, lock time and cold OS cache are not measured.']}
    (destination / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(destination / 'report.json')


if __name__ == '__main__':
    main()
