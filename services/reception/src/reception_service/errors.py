"""Content-free service errors shared by core and native integrations."""
from __future__ import annotations

VALIDATION_REASONS = frozenset({'sources_missing', 'number_unverified', 'commitment_unverified',
                                'reply_empty', 'question_repeated', 'reply_unverified'})


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
    from .integrations.accio_native.vendor.diagnostics import process_diagnostics, snapshot_diagnostics
    safe.update(snapshot_diagnostics(source))
    exception_class = process_diagnostics(source).get('exception_class')
    if exception_class:
        safe['exception_class'] = exception_class
    return safe


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, *, stage: str = 'service', diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.diagnostics = safe_diagnostics(diagnostics)

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': str(self), 'stage': self.stage, 'diagnostics': self.diagnostics}
