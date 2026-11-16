"""Reproducible synthetic message cursor benchmark; uses only a temporary database."""
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from reception_service.repository import Repository, encoded

INDEX = "CREATE INDEX entities_message_cursor ON entities(kind,json_extract(data,'$.conversation_id'),COALESCE(json_extract(data,'$.timestamp_ms'),-1),json_extract(data,'$.message_id'))"
QUERY = "SELECT data FROM entities WHERE kind=? AND json_extract(data,'$.conversation_id')=? AND (COALESCE(json_extract(data,'$.timestamp_ms'),-1),json_extract(data,'$.message_id')) < (?,?) ORDER BY COALESCE(json_extract(data,'$.timestamp_ms'),-1) DESC,json_extract(data,'$.message_id') DESC LIMIT ?"
PARAMS = ('message', 'c5', 900, 'm999999', 50)


def millis(start):
    return round((time.perf_counter() - start) * 1000, 3)


def measure(db):
    times = []
    output = None
    for _ in range(40):
        start = time.perf_counter()
        output = db.execute(QUERY, PARAMS).fetchall()
        times.append(millis(start))
    return {'median_ms': statistics.median(times), 'p95_ms': sorted(times)[37],
            'returned_rows': len(output), 'decoded_rows': len(output),
            'response_bytes': sum(len(row[0].encode('utf-8')) for row in output),
            'query_count_per_page': 1,
            'plan': [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + QUERY, PARAMS)],
            'ids': [json.loads(row[0])['message_id'] for row in output]}


def write_cost(db, offset):
    rows = [('message', 'extra%d' % (offset+i), encoded({'conversation_id': 'c5', 'timestamp_ms': i, 'message_id': 'extra%d' % (offset+i)})) for i in range(500)]
    start = time.perf_counter()
    db.execute('BEGIN IMMEDIATE')
    db.executemany('INSERT INTO entities VALUES (?,?,?)', rows)
    elapsed = millis(start)
    db.execute('ROLLBACK')
    return elapsed


def main():
    with tempfile.TemporaryDirectory() as directory:
        repo = Repository(Path(directory) / 'synthetic.sqlite3')
        db = repo.db
        db.execute('DROP INDEX entities_message_cursor')
        with repo.transaction():
            db.executemany('INSERT INTO entities VALUES (?,?,?)', [('message', 'm%06d' % i, encoded({'conversation_id': 'c%d' % (i % 20), 'timestamp_ms': i // 20, 'message_id': 'm%06d' % i, 'text': 'synthetic'})) for i in range(20000)])
        before = measure(db)
        write_before = write_cost(db, 0)
        start = time.perf_counter()
        db.execute(INDEX)
        create_ms = millis(start)
        after = measure(db)
        write_after = write_cost(db, 0)
        same = before.pop('ids') == after.pop('ids')
        report = {'conditions': {'platform': platform.platform(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'storage': 'temporary file, WAL, synchronous FULL', 'dataset': '20,000 synthetic messages, 20 conversations, 1,000 per conversation', 'runs': 40, 'cache': 'warm; OS and SQLite caches not flushed', 'write_test': '500 inserts in one rolled-back immediate transaction; excludes commit'}, 'before': before, 'after': after, 'equal_results': same, 'index_create_ms': create_ms, 'insert_500_before_ms': write_before, 'insert_500_after_ms': write_after, 'limits': 'Microbenchmark only; no snapshot, end-to-end detail, memory, cold read, concurrent accounts or real data measured.'}
        repo.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
