"""Content-free service errors shared by core and native integrations."""
from __future__ import annotations

VALIDATION_REASONS = frozenset({'sources_missing', 'number_unverified', 'commitment_unverified',
                                'reply_empty', 'question_repeated', 'reply_unverified'})


READ_STAGES = frozenset({'identity', 'native', 'source', 'network', 'http', 'parse', 'validation', 'decode', 'read', 'unknown'})
READ_CATEGORIES = frozenset({'size_limit', 'unsupported_type', 'type_mismatch', 'dimensions', 'animation', 'preview',
    'invalid_image', 'timeout', 'resolution', 'redirect', 'rejected', 'download', 'invalid_url', 'host_not_allowed',
    'unsafe_address', 'retrieval', 'status', 'content_encoding', 'invalid_json', 'invalid_document', 'invalid_seller',
    'verification', 'read_failed', 'business_failure', 'invalid_response', 'invalid_name', 'association', 'unknown'})


VISUAL_STAGES = frozenset({'preflight', 'workspace', 'process', 'read_trace', 'image_changed'})
VISUAL_STATUSES = frozenset({'invalid', 'unavailable', 'not_found', 'rejected', 'budget_exhausted',
                             'process_error', 'cancelled', 'completed'})


def read_diagnostics(error):
    stage = getattr(error, 'stage', 'unknown')
    category = getattr(error, 'category', 'association' if getattr(error, 'code', None) == 'photo_source_unverified' else 'unknown')
    return {'read_stage': stage if isinstance(stage, str) and stage in READ_STAGES else 'unknown',
            'read_category': category if isinstance(category, str) and category in READ_CATEGORIES else 'unknown'}


def safe_diagnostics(source):
    if not isinstance(source, dict):
        return {}
    allowed = {'elapsed_ms', 'exit_code', 'code', 'stage', 'result_source', 'native_error_category',
               'stdout_present', 'stderr_present', 'structured_error', 'recovery_status',
               'recovery_enabled', 'cli_timed_out', 'cli_terminated', 'cli_exit_code', 'cli_error_code',
               'validation_reason'}
    safe = {key: value for key, value in source.items()
            if key in allowed and type(value) in (str, bool, int, type(None))
            and (key != 'validation_reason' or value in VALIDATION_REASONS)}
    for key in ('queue_wait_ms', 'task_elapsed_ms', 'identity_ms', 'context_ms', 'photo_prepare_ms',
                'ai_elapsed_ms', 'submit_ms', 'receipt_ms', 'photo_candidates', 'photo_ready',
                'photo_failed', 'photo_pending', 'photo_bytes', 'image_reads'):
        value = source.get(key)
        if type(value) is int and 0 <= value <= 2 ** 53 - 1:
            safe[key] = value
    for key, allowed_values in (('visual_stage', VISUAL_STAGES), ('visual_status', VISUAL_STATUSES)):
        value = source.get(key)
        if isinstance(value, str) and value in allowed_values:
            safe[key] = value
    if source.get('cancel_reason') in ('source_changed', 'paused', 'shutdown', 'superseded'):
        safe['cancel_reason'] = source['cancel_reason']
    if isinstance(source.get('read_stage'), str) and source['read_stage'] in READ_STAGES:
        safe['read_stage'] = source['read_stage']
    if isinstance(source.get('read_category'), str) and source['read_category'] in READ_CATEGORIES:
        safe['read_category'] = source['read_category']
    from .integrations.accio_native.vendor.diagnostics import process_diagnostics, snapshot_diagnostics
    safe.update(snapshot_diagnostics(source))
    process = process_diagnostics(source)
    for key in ('exception_class', 'gate_reason', 'gate_checkpoint', 'token_acquired',
                'sync_target_version', 'sync_target_timestamp', 'authorization_epoch', 'source_version'):
        if key in process:
            safe[key] = process[key]
    return safe


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, *, stage: str = 'service', diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.diagnostics = safe_diagnostics(diagnostics)

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': str(self), 'stage': self.stage, 'diagnostics': self.diagnostics}
