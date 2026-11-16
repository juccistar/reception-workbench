"""Offline integration measurements against real service objects and disposable data."""
import asyncio
from contextlib import closing
import gc
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'test_support'))
from reception_test_support.service import create_synthetic_service
from reception_service.repository import encoded
from reception_service.reception_state import ReceptionProjection
from reception_service.model_providers.runtime import ModelRuntime
from reception_service.whatsapp_account_service import WhatsAppAccountService


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000


def measure(service, fn):
    statements = []
    decoded = []
    original = json.loads
    def loads(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if isinstance(result, dict):
            decoded.append(result)
        return result
    service.repo.db.set_trace_callback(statements.append)
    try:
        with patch('json.loads', side_effect=loads):
            value, elapsed = timed(fn)
    finally:
        service.repo.db.set_trace_callback(None)
    return value, {'ms': round(elapsed, 3), 'sql_statements': len(statements),
                   'sql_selects': sum(s.lstrip().upper().startswith('SELECT') for s in statements),
                   'decoded_json_objects': len(decoded),
                   'decoded_message_objects': sum('message_id' in x and 'timestamp_ms' in x and 'conversation_id' in x for x in decoded),
                   'response_utf8_bytes': len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))}


def distribution(rows):
    times = sorted(x['ms'] for x in rows)
    return {'median_ms': round(statistics.median(times), 3), 'p95_ms': times[(95 * len(times) + 99) // 100 - 1],
            'runs': len(times), 'last_counts': {k: v for k, v in rows[-1].items() if k != 'ms'}}


def service_measure(base):
    os.environ['RECEPTION_MODEL_BUDGET_DIRECTORY'] = str(base / 'service-budget')
    s = create_synthetic_service(base / 'service')
    try:
        with s.repo.transaction():
            template = s.repo.list('conversation')[0]
            for i in range(20):
                cid = 'perf-%02d' % i
                c = dict(template, conversation_id=cid, buyer_id='buyer-%02d' % i, customer_name='Synthetic %02d' % i, case_id=None)
                s.repo.put('conversation', cid, c)
                for j in range(300):
                    mid = '%s-m%04d' % (cid, j)
                    m = {'message_id': mid, 'seller_id': s.seller_id, 'buyer_id': c['buyer_id'], 'conversation_id': cid,
                         'role': 'buyer', 'text': 'synthetic message %d' % j, 'timestamp_ms': 100000+j, 'attachments': [], 'own': False}
                    s.repo.put('message', encoded([cid, mid]), m)
        report = {}
        for name, fn in [('snapshot', s.get_snapshot), ('conversation', lambda: s.get_conversation('perf-00'))]:
            result, first = measure(s, fn)
            rows = [measure(s, fn)[1] for _ in range(20)]
            report[name] = {'first_after_seed': first, 'warm': distribution(rows)}
            if name == 'snapshot':
                assert len(result['conversations']) == 23
            else:
                assert result['conversation']['conversation_id'] == 'perf-00'
                assert len(result['messages']) == 100
                assert all(m['conversation_id'] == 'perf-00' for m in result['messages'])
        with s.repo.transaction():
            for j in range(300):
                mid = 'extra-%04d' % j
                s.repo.put('message', encoded(['perf-01', mid]), {'message_id': mid, 'seller_id': s.seller_id,
                    'buyer_id': 'buyer-01', 'conversation_id': 'perf-01', 'role': 'buyer', 'text': 'extra other customer',
                    'timestamp_ms': 200000+j, 'attachments': [], 'own': False})
        _, other_growth = measure(s, lambda: s.get_conversation('perf-00'))
        report['detail_after_300_more_messages_in_other_customer'] = other_growth
        tracemalloc.start()
        samples = []
        for cycle in range(4):
            for i in range(20):
                s.get_conversation('perf-%02d' % i)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            samples.append({'switches': (cycle+1)*20, 'python_current_bytes_after_gc': current, 'python_peak_bytes': peak})
        tracemalloc.stop()
        report['switch_memory'] = samples
        report['blocked_model'] = blocked_model(s, base)
        return report
    finally:
        s.close()


def legacy_reply_pending(projection, conversation):
    cid = conversation['conversation_id']
    messages = projection.service._messages(cid)
    buyer = [m for m in messages if m.get('role') == 'buyer']
    seller = [m for m in messages if m.get('role') == 'seller']
    result = bool(buyer) and (any(m.get('timestamp_ms') is None for m in buyer) or not seller
        or seller[-1].get('timestamp_ms') is None or any(m.get('timestamp_ms') is not None
        and m['timestamp_ms'] > seller[-1]['timestamp_ms'] for m in buyer))
    if buyer and any(o.get('status') == 'accepted' and o.get('source_message_id') == buyer[-1]['message_id']
                     for o in projection.operations[cid]):
        result = False
    return bool(result)


def case_measure(base, old_schema):
    os.environ['RECEPTION_MODEL_BUDGET_DIRECTORY'] = str(base / ('old-case-budget' if old_schema else 'new-case-budget'))
    s = create_synthetic_service(base / ('old-case' if old_schema else 'new-case'))
    try:
        if old_schema:
            for (name,) in s.repo.db.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql LIKE '%timestamp_ms%'").fetchall():
                s.repo.db.execute('DROP INDEX "' + name.replace('"', '""') + '"')
        template = s.repo.list('conversation')[0]
        case_template = s.repo.list('case')[0]
        with s.repo.transaction():
            for i in range(20):
                cid, case_id, buyer = 'case-perf-%02d' % i, 'case-%02d' % i, 'buyer-%02d' % i
                s.repo.put('conversation', cid, dict(template, conversation_id=cid, buyer_id=buyer, case_id=case_id,
                                                     last_buyer_at=100299))
                s.repo.put('case', case_id, dict(case_template, case_id=case_id, conversation_id=cid, buyer_id=buyer,
                                                seller_id=s.seller_id, state='clarifying'))
                for j in range(300):
                    mid = '%s-m%04d' % (cid, j)
                    s.repo.put('message', encoded([cid, mid]), {'message_id': mid, 'seller_id': s.seller_id,
                        'buyer_id': buyer, 'conversation_id': cid, 'role': 'buyer', 'text': 'synthetic message %d' % j,
                        'timestamp_ms': 100000+j, 'attachments': [], 'own': False})
        report = {}
        for name, fn in [('snapshot', s.get_snapshot), ('conversation', lambda: s.get_conversation('case-perf-00'))]:
            with patch.object(ReceptionProjection, 'reply_pending', legacy_reply_pending):
                old_result, _ = measure(s, fn)
                before = [measure(s, fn)[1] for _ in range(20)]
            new_result, _ = measure(s, fn)
            after = [measure(s, fn)[1] for _ in range(20)]
            # Volatile timestamps and background jobs can vary; verify the actual
            # state and pending results for all seeded customers explicitly.
            old_conversations = old_result['conversations'] if name == 'snapshot' else [old_result['conversation']]
            new_conversations = new_result['conversations'] if name == 'snapshot' else [new_result['conversation']]
            def stable(rows):
                return {r['conversation_id']: (r['reply_pending'], r['reception_status']['state'],
                        r['reception_status']['owner'], r['reception_status']['blockers']) for r in rows
                        if r['conversation_id'].startswith('case-perf-')}
            assert stable(old_conversations) == stable(new_conversations), (stable(old_conversations), stable(new_conversations))
            report[name] = {'before_reply_projection': distribution(before), 'after_reply_projection': distribution(after)}
        return report
    finally:
        s.close()


def blocked_model(s, base):
    directory = base / 'blocked-budget'
    directory.mkdir()
    runtime = ModelRuntime(cleanup_timeout=.15, budget_directory=str(directory))
    entered, release, cancel, finished = (threading.Event() for _ in range(4))
    errors = []
    def blocking():
        entered.set()
        if not release.wait(15):
            raise RuntimeError('worker timeout')
    async def operation():
        from reception_service.model_providers.owned_sync import owned_sync
        await owned_sync(blocking, complete_on_cancel=True)
    def run():
        try:
            runtime.run('reception', operation, cancel=cancel, deadline=12, on_finished=finished.set)
        except Exception as exc:
            errors.append(getattr(exc, 'code', type(exc).__name__))
    t = threading.Thread(target=run)
    t.start()
    try:
        if not entered.wait(5):
            raise RuntimeError('model worker did not start: %r' % errors)
        def count():
            with closing(sqlite3.connect(directory / 'budget.sqlite3')) as db:
                return db.execute('SELECT count(*) FROM requests WHERE granted=1').fetchone()[0]
        before = count()
        _, query = timed(s.get_snapshot)
        _, save = timed(lambda: s.save_conversation_supplement('perf-00', 'synthetic terms', None, 'perf-save'))
        _, pause = timed(lambda: s.set_reception(False))
        during = count()
        assert before == during == 1
        cancel.set()
        t.join(3)
        assert not t.is_alive() and errors and 'model_cleanup_timeout' in errors[0]
        after_cancel = count()
        assert after_cancel == 1
        release.set()
        assert finished.wait(5), 'real model cleanup did not finish'
        runtime.close()
        after = count()
        assert after == 0
        return {'snapshot_ms': round(query, 3), 'save_ms': round(save, 3), 'pause_ms': round(pause, 3),
                'granted_before': before, 'granted_while_blocked_after_local_operations': during,
                'granted_after_cancellation_before_worker_exit': after_cancel, 'granted_after_worker_exit': after,
                'cancellation_result': errors[0],
                'note': 'Independent real ModelRuntime with blocked owned synchronous worker; no provider or network. Pause does not cancel this independent worker; cancellation is separately exercised.'}
    finally:
        release.set()
        t.join(16)
        if entered.is_set():
            finished.wait(5)
        runtime.close()


def accounts(base):
    os.environ['RECEPTION_MODEL_BUDGET_DIRECTORY'] = str(base / 'accounts-budget')
    services = [WhatsAppAccountService(base / ('wa%d' % i), str(uuid.uuid4()), lambda event: None) for i in range(2)]
    try:
        for i, s in enumerate(services):
            account, epoch = 'account-%d' % i, 'epoch-%d' % i
            s._wa_set_account(account, epoch, 'connected')
            s._wa_ingest(account, epoch, [{'id': 'm%d' % i, 'chat_id': 'same-chat', 'body': 'account-%d body' % i,
                'timestamp_ms': 100, 'from_me': False, 'name': 'Synthetic', 'is_group': False, 'is_history': False}])
        for i, s in enumerate(services):
            messages = s.get_whatsapp_context('same-chat')['whatsapp_messages']
            assert [m['text'] for m in messages] == ['account-%d body' % i]
        barrier = threading.Barrier(2)
        results = [None, None]
        def run(i):
            s = services[i]
            barrier.wait(timeout=5)
            start = time.perf_counter()
            for _ in range(20):
                state = s.get_whatsapp_state()
                s.takeover_whatsapp('same-chat', True, 'account-%d' % i, 'epoch-%d' % i)
            results[i] = {'elapsed_ms': round((time.perf_counter()-start)*1000, 3), 'account': state['account_id']}
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(15)
        assert all(not t.is_alive() for t in threads) and all(results)
        assert [r['account'] for r in results] == ['account-0', 'account-1']
        return {'parallel': results, 'iterations_each': 20, 'note': 'Two real account services, separate disposable workspaces, identical chat IDs; concurrent state reads and takeover writes.'}
    finally:
        for s in services: s.close()


def main():
    with tempfile.TemporaryDirectory(prefix='rw042-integration-') as tmp:
        base = Path(tmp)
        result = {'conditions': {'platform': platform.platform(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'dataset': '20 synthetic conversations x 300 messages plus 3 fixture conversations',
            'cache': 'first call after seeding is not OS-cold; warm 20 successive calls',
            'storage': 'temporary SQLite WAL; no real accounts, model, or credentials',
            'decode_metric': 'json.loads calls yielding objects; message count identifies objects by keys, not SQLite scanned rows',
            'memory': 'tracemalloc Python allocations after gc; excludes native SQLite, RSS and other processes'},
            'service': service_measure(base), 'case_customers_new_schema': case_measure(base, False),
            'case_customers_old_schema_without_cursor_index': case_measure(base, True), 'whatsapp_accounts': accounts(base),
            'baseline': {'status': 'not_measured', 'reason': 'No isolated f8339ad source/dependency comparison was executed; no speedup claim.'},
            'limitations': ['Synthetic local service calls are not end-to-end UI, cold OS boot, real accounts, provider latency or model quality.',
              'SQL trace counts statements on workspace connection only; sql_selects counts statements beginning SELECT and excludes WITH queries. JSON decoding count includes any instrumented Python JSON objects and does not count SQLite internal scans.',
              'Case measurements compare the old reply-pending algorithm with the new one on the same dataset and current service source; they do not isolate or claim any service-layer automatic-source optimization.',
              'Blocked runtime is independent of the service generation task; service pause and cancellation are not tested as a single service-owned generation lifecycle.']}
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
