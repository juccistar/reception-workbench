"""Create and inspect synthetic account databases for the frozen-backend probe."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


def key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def main():
    action, root_arg, workspace, scenario = sys.argv[1:]
    root = Path(root_arg).resolve()
    # The caller creates and owns this temporary directory; refuse arbitrary paths.
    if not (root / '.prompt-revision-042-probe').is_file():
        raise ValueError('missing temporary-root marker')
    db = root / scenario / 'whatsapp-accounts' / workspace / 'account.sqlite3'
    if action == 'seed':
        db.parent.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(db) as con:
            con.execute('CREATE TABLE records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id))')
            con.execute('CREATE TABLE metadata (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
            def put(kind, ident, data):
                con.execute('INSERT INTO records VALUES (?,?,?)', (kind, ident, json.dumps(data)))
            account, chat, segment = 'synthetic-account', 'synthetic-chat', 'synthetic-segment'
            source = key(account, chat, 'buyer')
            basis = dict(account_id=account, connection_epoch='old-epoch', chat_id=chat, merchant_id='synthetic-merchant', pending_message_id='buyer', segment_id=segment, binding_revision=1, source_fingerprint='synthetic-source-fingerprint', batch_id='batch')
            part = dict(body='Synthetic old reply', status='prepared', part_index=0, operation_id='head', message_id=None)
            head = dict(basis, operation_id='head', part_index=0, body=part['body'], status='prepared', submission_started=False, message_id=None, ack=None, owner_run_id='previous-process')
            parts = [part]
            if scenario in ('unknown', 'accepted'):
                head.update(status=scenario, submission_started=True)
                part['status'] = scenario
            if scenario == 'accepted':
                head.update(message_id='synthetic-platform-id', ack=1)
                part['message_id'] = 'synthetic-platform-id'
                parts.append(dict(body='Synthetic tail', status='prepared', part_index=1, operation_id='tail', message_id=None))
                put('operation', 'tail', dict(basis, operation_id='tail', part_index=1, body='Synthetic tail', status='prepared', submission_started=False, message_id=None, ack=None, owner_run_id='previous-process'))
            put('meta', 'workspace', dict(workspace_id=workspace))
            put('operation', 'head', head)
            put('reply_batch', 'batch', dict(basis, parts=parts, next_index=1 if scenario == 'accepted' else 0, status='prepared'))
            put('generation_attempt', source, dict(status='completed', owner_run_id='previous-process', attempt_id='original-attempt', source_fingerprint=basis['source_fingerprint'], count=3 if scenario == 'count3' else 1))
            put('binding', key(account, chat), dict(merchant_id='synthetic-merchant', revision=1, segment_id=segment))
            put('conversation', key(account, chat), dict(account_id=account, chat_id=chat, pending=True, pending_message_id='buyer'))
    elif action == 'read':
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
            con.execute('PRAGMA query_only=ON')
            rows = con.execute("SELECT kind,id,data FROM records WHERE kind IN ('operation','reply_batch','generation_attempt') ORDER BY kind,id").fetchall()
            print(json.dumps({kind + ':' + ident: json.loads(data) for kind, ident, data in rows}))
    else:
        raise ValueError('invalid action')


if __name__ == '__main__':
    main()
