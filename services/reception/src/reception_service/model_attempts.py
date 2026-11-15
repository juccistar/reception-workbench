"""Durable, source-scoped admission for model HTTP calls."""
import contextvars
import hashlib
import math
import time
import uuid

from .errors import ServiceError
from .repository import encoded, now_ms

_KIND = 'model_attempt_chain_v1'
_DEADLINE = contextvars.ContextVar('model_attempt_deadline', default=None)


def _deadline(previous):
    values = [v for v in (previous.get('deadline_ms'), _DEADLINE.get()) if type(v) is int]
    deadline = min(values) if values else None
    if deadline is not None and now_ms() >= deadline:
        raise ServiceError('model_attempt_expired', '该来源的模型请求已超过总期限', stage='generation')
    if previous.get('retry_at_ms', 0) > now_ms():
        raise ServiceError('model_rate_limited', '模型要求等待后再试', stage='generation')
    return deadline


class AttemptLifecycle:
    """A callable admission whose completion belongs to the real runtime task.

    An outer caller may return while its callback worker still owns a claim.
    Only the router's actual task finishes managed claims; fallback is for an
    explicitly injected synchronous integration that does not use the router.
    """
    def __init__(self, repo, start, tokens):
        self.repo, self.start, self.tokens = repo, start, tokens
        self.managed = False
        self.deadline = None
        self.deadline_ms = None
        self.had_attempt = False

    def bind(self, deadline):
        self.managed = True
        self.deadline = deadline
        self.deadline_ms = now_ms() + max(0, int((deadline - time.monotonic()) * 1000))

    def __call__(self):
        marker = _DEADLINE.set(self.deadline_ms)
        try:
            self.start()
            self.had_attempt |= bool(self.tokens)
            deadlines = [self.repo.get(_KIND, key, {}).get('deadline_ms') for key in self.tokens]
            deadlines = [value for value in deadlines if type(value) is int]
            if deadlines:
                self.deadline_ms = min(deadlines)
                persisted_end = time.monotonic() + max(0, (self.deadline_ms - now_ms()) / 1000)
                self.deadline = min(self.deadline, persisted_end) if self.deadline is not None else persisted_end
        finally:
            _DEADLINE.reset(marker)

    def complete(self, outcome, *, no_http=False, retry_after=None):
        finish(self.repo, self.tokens, outcome=outcome, no_http=no_http, retry_after=retry_after)
        self.tokens.clear()

    def fallback(self, outcome):
        if not self.managed:
            self.complete(outcome)


def job_blocks_replay(repo, job):
    """Legacy started jobs remain barriers; only exact new no-submit evidence opens one."""
    if not job.get('external_started'):
        return False
    keys = job.get('model_attempt_chain_ids')
    if not isinstance(keys, list) or not keys:
        return True
    for key in keys:
        row = repo.get(_KIND, key, {})
        if (row.get('status') != 'unsubmitted' or row.get('attempts', 2) >= 2
                or row.get('retry_at_ms', 0) > now_ms()
                or type(row.get('deadline_ms')) is not int or row['deadline_ms'] <= now_ms()):
            return True
    return False


def chain_id(seller_id, purpose, channel, source):
    if not all(isinstance(value, str) and value for value in (seller_id, purpose, channel)) or not source:
        raise ValueError('A stable model attempt source is required')
    return hashlib.sha256(encoded([seller_id, purpose, channel, source]).encode('utf-8')).hexdigest()


def claim_many(repo, *, seller_id, purpose, channel, sources):
    """Atomically claim every source; return {chain_id: token}, or claim none."""
    ids = sorted({chain_id(seller_id, purpose, channel, source) for source in sources})
    if not ids:
        raise ValueError('At least one source is required')
    tokens = {}
    with repo.transaction():
        for key in ids:
            row = repo.get(_KIND, key)
            if row and (row.get('status') != 'unsubmitted' or row.get('attempts', 0) >= 2):
                raise ServiceError('model_attempt_exhausted', '该来源的模型请求已处理或结果未知，不能自动重放', stage='generation')
        for key in ids:
            previous = repo.get(_KIND, key) or {}
            token = uuid.uuid4().hex
            row = dict(previous)
            row.update(attempts=previous.get('attempts', 0) + 1, status='in_flight', token=token,
                       deadline_ms=_deadline(previous), updated_at=now_ms())
            repo.put(_KIND, key, row, history=True)
            tokens[key] = token
    return tokens


def claim(repo, *, seller_id, purpose, channel, source):
    return claim_many(repo, seller_id=seller_id, purpose=purpose, channel=channel, sources=[source])


def claim_whatsapp(repo, *, seller_id, account_id, chat_id, pending_message_id, legacy_id):
    """Migrate the old WA chain and claim the same source in one transaction."""
    source = {'account_id': account_id, 'chat_id': chat_id, 'pending_message_id': pending_message_id}
    key = chain_id(seller_id, 'reception', 'whatsapp', source)
    tokens = {}
    blocked = False
    with repo.transaction():
        old = repo.get('wa_generation_attempt_chain', legacy_id)
        row = repo.get(_KIND, key)
        if old and not (row or {}).get('wa_legacy_migrated'):
            legacy_count = old.get('attempts')
            if type(legacy_count) is not int or legacy_count < 0:
                legacy_count = 2
            row = dict(row or {})
            row['attempts'] = row.get('attempts', 0) + legacy_count
            # Old completed calls cannot be treated as definite non-submissions.
            legacy_status = ('unknown' if old.get('in_flight') or old.get('status') in ('in_flight', 'unknown')
                             else 'completed' if legacy_count else None)
            if legacy_status:
                row['wa_legacy_terminal'] = legacy_status
                if row.get('status') != 'in_flight':
                    row['status'] = legacy_status
            else:
                row.setdefault('status', 'unsubmitted')
            row['wa_legacy_migrated'] = True
            row['updated_at'] = now_ms()
            repo.put(_KIND, key, row, history=True)
        if row and (row.get('status') != 'unsubmitted' or row.get('attempts', 0) >= 2):
            blocked = True
        else:
            token = uuid.uuid4().hex
            row = dict(row or {})
            row.update(attempts=row.get('attempts', 0) + 1, status='in_flight', token=token,
                       deadline_ms=_deadline(row), updated_at=now_ms())
            repo.put(_KIND, key, row, history=True)
            tokens[key] = token
    if blocked:
        raise ServiceError('model_attempt_exhausted', '该来源的模型请求已处理或结果未知，不能自动重放', stage='generation')
    return tokens


def finish(repo, tokens, *, outcome, no_http=False, retry_after=None):
    """Finish only matching in-flight tokens; unknown is never replayable."""
    if outcome not in ('completed', 'unknown', 'unsubmitted', 'failed'):
        raise ValueError('Invalid model attempt outcome')
    with repo.transaction():
        for key, token in tokens.items():
            row = repo.get(_KIND, key)
            if row and row.get('status') == 'in_flight' and row.get('token') == token:
                if no_http and outcome == 'unsubmitted':
                    row['attempts'] = max(0, row.get('attempts', 1) - 1)
                if outcome == 'unsubmitted' and retry_after is not None:
                    row['retry_at_ms'] = now_ms() + math.ceil(max(0, retry_after) * 1000)
                row.update(status=row.get('wa_legacy_terminal') if outcome == 'unsubmitted' and row.get('wa_legacy_terminal') else outcome,
                           no_http=no_http, updated_at=now_ms())
                repo.put(_KIND, key, row, history=True)
