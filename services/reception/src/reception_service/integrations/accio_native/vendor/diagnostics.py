"""Allowlisted connection diagnostics shared by UI receipts and CLI reports."""
from __future__ import annotations

import re

STAGES = frozenset({"agent_selection", "identity", "context", "cli", "answer", "schema", "analysis", "complete"})
NATIVE_ERROR_CATEGORIES = frozenset({
    'internal_error', 'backend_timeout', 'rate_limit',
    'desktop_response_timeout', 'io_timeout', 'cli_unclassified_error',
})
CODES = NATIVE_ERROR_CATEGORIES | frozenset({
    "ok", "adapter_error", "send_receipt_invalid", "agent_missing", "agent_metadata_invalid", "agent_ambiguous",
    "identity_invalid", "context_too_long", "cli_invalid", "analysis_invalid",
    "spawn", "timeout", "encoding", "exit", "non_json", "native_error",
    "tool_approval", "agent_selection", "auth", "gateway", "args",
    "answer_non_json", "answer_missing", "schema_invalid", "schema_action",
    "schema_text", "schema_reply", "schema_lists", "schema_procurement",
    "schema_questions", "schema_fact_keys", "schema_facts", "schema_fact_value",
    "schema_evidence_ids", "schema_evidence_quote", "schema_reask",
    "schema_question_limit", "schema_incomplete",
})


def safe_snapshot_message_id(value):
    """Permit known numeric platform IDs; never relay an arbitrary string ID."""
    if type(value) is int and value > 0:
        value = str(value)
    if type(value) is str and (value == 'redacted' or re.fullmatch(r'[1-9][0-9]{0,31}(?:\.PNM)?', value)):
        return value
    return None


def snapshot_diagnostics(source):
    """Copy only bounded, content-free facts about one snapshot row."""
    if not isinstance(source, dict):
        return {}
    safe = {}
    message_id = source.get('snapshot_message_id')
    if type(message_id) is str and safe_snapshot_message_id(message_id) is not None:
        safe['snapshot_message_id'] = message_id
    enums = {
        'snapshot_difference': {'role', 'role_conflict', 'content', 'role_and_content', 'sender_identity',
                                'time', 'time_and_content'},
        'snapshot_from': {'buyer', 'seller', 'unknown', 'invalid', 'missing'},
        'snapshot_sender_role': {'buyer', 'seller', 'unknown', 'invalid', 'missing'},
        'snapshot_content_type': {'string', 'null', 'object', 'array', 'other'},
    }
    for key, values in enums.items():
        value = source.get(key)
        if type(value) is str and value in values:
            safe[key] = value
    for key in ('snapshot_from_present', 'snapshot_sender_role_present', 'snapshot_role_equal', 'snapshot_content_equal'):
        if type(source.get(key)) is bool:
            safe[key] = source[key]
    for key in ('snapshot_content_length', 'snapshot_platform_content_length'):
        length = source.get(key)
        if type(length) is int and 0 <= length <= 1000000:
            safe[key] = length
    return safe


def process_diagnostics(source):
    """Copy only content-free, strictly typed process facts."""
    if not isinstance(source, dict):
        return {}
    safe = snapshot_diagnostics(source)
    elapsed = source.get('elapsed_ms')
    if type(elapsed) is int and elapsed >= 0:
        safe['elapsed_ms'] = elapsed
    for key in ('stdout_present', 'stderr_present', 'structured_error'):
        if type(source.get(key)) is bool:
            safe[key] = source[key]
    category = source.get('native_error_category')
    if isinstance(category, str) and category in NATIVE_ERROR_CATEGORIES:
        safe['native_error_category'] = category
    if source.get('result_source') in ('stdout', 'completed_record'):
        safe['result_source'] = source['result_source']
    for key in ('recovery_enabled', 'cli_timed_out', 'cli_terminated'):
        if type(source.get(key)) is bool:
            safe[key] = source[key]
    if type(source.get('cli_exit_code')) is int:
        safe['cli_exit_code'] = source['cli_exit_code']
    if source.get('recovery_status') in ('disabled', 'unavailable', 'not_found', 'rejected', 'budget_exhausted', 'completed'):
        safe['recovery_status'] = source['recovery_status']
    reason = source.get('validation_reason')
    if isinstance(reason, str) and reason in ('sources_missing', 'number_unverified', 'commitment_unverified',
                                             'reply_empty', 'question_repeated', 'reply_unverified'):
        safe['validation_reason'] = reason
    code = source.get('cli_error_code')
    if isinstance(code, str) and code in CODES:
        safe['cli_error_code'] = code
    exception_class = source.get('exception_class')
    if exception_class in ('AdapterError', 'OSError', 'ValueError', 'TypeError', 'KeyError',
                           'JSONDecodeError', 'UnicodeDecodeError', 'UnicodeEncodeError',
                           'FileNotFoundError', 'FileExistsError', 'PermissionError',
                           'IsADirectoryError', 'NotADirectoryError'):
        safe['exception_class'] = exception_class
    return safe


def connection_diagnostics(result):
    source = result.get("diagnostics") if isinstance(result, dict) else None
    if not isinstance(source, dict):
        return {}
    safe = {}
    for key, allowed in (("stage", STAGES), ("code", CODES)):
        value = source.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    value = source.get("exit_code", source.get("returncode"))
    if type(value) is int:
        safe["exit_code"] = value
    safe.update(process_diagnostics(source))
    return safe
