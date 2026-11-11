"""Read-only job projection for the ordinary activity list; scheduling uses raw jobs."""
from __future__ import annotations

import json
from datetime import datetime

from .integrations.accio_native.vendor.diagnostics import CODES
from .repository import Repository


# Only message reads can be background activity. A present false (or null) flag
# must never be mistaken for a missing flag on a pre-flag monitor job.
_BACKGROUND_SQL = """
    COALESCE(json_extract(data, '$.kind') = 'sync_messages' AND (
        json_type(data, '$.background_sync') = 'true' OR (
            json_type(data, '$.background_sync') IS NULL AND
            json_type(data, '$.sync_generation') IS NOT NULL
        )
    ), 0)
"""

# Reuse the native boundary's content-free enum and the core read error names.
# Never echo arbitrary error.code values, even if they look like identifiers.
_ERROR_CODES = tuple(sorted((CODES - {'ok'}) | {
    'native_failed', 'native_timeout', 'cli_timeout', 'summary_failed', 'task_failed', 'runtime_blocked', 'identity_mismatch',
    'seller_mismatch', 'identity_unverified', 'invalid_page', 'invalid_cursor',
    'invalid_request', 'history_limit', 'history_cursor_stalled',
    'summary_cursor_stalled', 'message_unverified',
    'sync_failed', 'sync_unknown',
}))
_ERROR_MESSAGE = '后台消息读取异常，请检查连接后重试'


def visible_jobs(repo: Repository) -> list[dict]:
    """Keep business jobs intact and summarize real background read errors.

    Both queries share the repository lock. Filtering, code normalization and
    aggregation happen in SQLite; routine background payloads are never decoded
    in Python. Neither current rows nor revision/receipt history is rewritten.
    """
    placeholders = ','.join('?' for _ in _ERROR_CODES)
    with repo.lock:
        rows = repo.db.execute(f"""
            SELECT data FROM entities
            WHERE kind = 'job' AND NOT ({_BACKGROUND_SQL})
            ORDER BY json_extract(data, '$.created_at') DESC, rowid
        """).fetchall()
        errors = repo.db.execute(f"""
            WITH failures AS (
                SELECT id,
                    CASE
                        WHEN json_type(data, '$.error.code') = 'text'
                             AND json_extract(data, '$.error.code') IN ({placeholders})
                            THEN json_extract(data, '$.error.code')
                        WHEN json_extract(data, '$.status') = 'unknown' THEN 'sync_unknown'
                        ELSE 'sync_failed'
                    END AS code,
                    CASE WHEN json_type(data, '$.created_at') = 'integer'
                         THEN json_extract(data, '$.created_at') ELSE 0 END AS first_at,
                    CASE WHEN json_type(data, '$.completed_at') = 'integer'
                         THEN json_extract(data, '$.completed_at')
                         WHEN json_type(data, '$.created_at') = 'integer'
                         THEN json_extract(data, '$.created_at') ELSE 0 END AS last_at
                FROM entities
                WHERE kind = 'job' AND ({_BACKGROUND_SQL})
                    AND json_extract(data, '$.status') IN ('failed', 'unknown')
                    AND NOT (
                        json_type(data, '$.background_sync') IS NULL
                        AND json_extract(data, '$.status') = 'failed'
                        AND COALESCE(json_extract(data, '$.error.code'), '') = ''
                        AND json_type(data, '$.result.changed_count') IS NOT NULL
                    )
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY code ORDER BY last_at DESC, first_at DESC, id DESC
                ) AS position FROM failures
            )
            SELECT code, COUNT(*), MIN(first_at), MAX(last_at),
                   MAX(CASE WHEN position = 1 THEN id END)
            FROM ranked GROUP BY code ORDER BY code
        """, _ERROR_CODES).fetchall()

    jobs = [json.loads(row[0]) for row in rows]
    for code, count, first_at, last_at, last_job_id in errors:
        try:
            last_time = datetime.fromtimestamp(last_at / 1000).strftime('%m/%d %H:%M')
        except (ValueError, OverflowError, OSError):
            last_time = '时间待核实'
        jobs.append({
            'job_id': f'sync-error:{code}',
            'kind': 'sync_messages',
            'background_sync': True,
            'conversation_id': None,
            'case_id': None,
            'status': 'failed',
            'stage': 'sync_messages',
            'message': f'{_ERROR_MESSAGE}；累计 {count} 次；最后发生时间：{last_time}',
            'created_at': first_at,
            'started_at': None,
            'completed_at': last_at,
            'error': {'code': code, 'message': _ERROR_MESSAGE},
            'diagnostics': {'count': count, 'last_job_id': last_job_id},
        })
    return sorted(jobs, key=lambda job: job['created_at'], reverse=True)
