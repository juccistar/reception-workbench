"""Read only projected check receipts. Never instantiate the business repository."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

DIAGNOSTICS = ('elapsed_ms', 'exit_code', 'code', 'stage', 'result_source', 'native_error_category',
               'stdout_present', 'stderr_present', 'structured_error', 'recovery_status', 'recovery_enabled',
               'cli_timed_out', 'cli_terminated', 'cli_exit_code', 'cli_error_code', 'validation_reason')


def read_checks(data_root: Path, seller_id: str) -> dict:
    if not re.fullmatch(r'[1-9][0-9]*', seller_id):
        raise ValueError('seller_id must contain positive ASCII digits')
    seller_hash = hashlib.sha256(seller_id.encode('utf-8')).hexdigest()
    database = Path(data_root) / 'live' / 'sellers' / seller_hash / 'workspace.sqlite3'
    report = {'observed_at': datetime.now(timezone.utc).isoformat(), 'seller_suffix': seller_id[-6:],
              'read_only': True, 'current_reception_status': 'not_observed',
              'note': 'Historical check receipts only; current reception state must be read from the live window.',
              'database_present': database.is_file(), 'checks': []}
    if not database.is_file():
        return report
    fields = ['job_id', 'kind', 'status', 'stage', 'created_at', 'started_at', 'completed_at']
    columns = ["json_extract(data, '$." + name + "')" for name in fields]
    columns += ["json_extract(data, '$.error.code')"]
    columns += ["json_extract(data, '$.diagnostics." + name + "')" for name in DIAGNOSTICS]
    query = ('SELECT ' + ','.join(columns) + " FROM entities WHERE kind='job' "
             "AND json_extract(data,'$.kind') IN ('check_connection','check_ai','preview_send','check_feishu') "
             "ORDER BY json_extract(data,'$.created_at') DESC LIMIT 20")
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
        connection.execute('PRAGMA query_only=ON')
        for row in connection.execute(query):
            record = dict(zip(fields, row[:len(fields)]))
            record['error_code'] = row[len(fields)]
            record['diagnostics'] = {name: value for name, value in zip(DIAGNOSTICS, row[len(fields) + 1:]) if value is not None}
            report['checks'].append(record)
    return report


def main():
    parser = argparse.ArgumentParser(description='Read safe, historical live-check receipts only')
    parser.add_argument('--seller-id', required=True)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AlibabaReceptionWorkbenchV2')
    args = parser.parse_args()
    try:
        report = read_checks(args.data_dir, args.seller_id)
    except (ValueError, sqlite3.Error, OSError):
        report = {'read_only': True, 'error_code': 'receipt_read_unavailable', 'current_reception_status': 'not_observed'}
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
