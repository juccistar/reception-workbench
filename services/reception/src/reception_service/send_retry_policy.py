"""Bound automatic send attempts using durable operation and job evidence."""

# Only explicit, pre-submission transport availability failures qualify.
TRANSIENT_CODES = frozenset({'backend_timeout', 'rate_limit', 'io_timeout', 'timeout'})
BACKOFF_MS = (5000, 15000)


def retry_message(state, attempts, retry_at, now):
    if state == 'waiting':
        seconds = max(0, (retry_at - now + 999) // 1000)
        return f'第{attempts}次发送未提交，将在约{seconds}秒后进行第{attempts + 1}次尝试（最多3次）'
    if state == 'exhausted':
        return f'已尝试{attempts}次，达到最多3次的限制，已停止自动重试'
    return '发送未提交，原因无法安全自动重试，已停止本条自动重试'


def chain_attempts(repo, seller_id, conversation_id, source_id, phase='reply', result_version=None, *, part_id=None):
    jobs = {j['job_id']: j for j in repo.list('job', conversation_id=conversation_id)}
    attempts = []
    for op in repo.list('operation', conversation_id=conversation_id):
        job = jobs.get(op.get('job_id'), {})
        if (op.get('seller_id') != seller_id or op.get('source_message_id') != source_id
                or not job.get('automatic') or op.get('kind') != 'send'
                or part_id is not None and op.get('part_id') != part_id):
            continue
        intent = op.get('intent') or {}
        op_phase = intent.get('continuity_phase', 'reply') if op.get('purpose') == 'continuity' else 'reply'
        if op_phase != phase:
            continue
        if phase in ('assistance', 'supplement'):
            plan = repo.get('continuity_plan', op.get('plan_id')) or {}
            if phase == 'assistance' and plan.get('result_version') != result_version:
                continue
            if phase == 'supplement' and not set(plan.get('supplement_issue_ids', [])) & set(result_version or []):
                continue
        attempts.append(op)
    attempts.sort(key=lambda o: o.get('updated_at', 0))
    return attempts


def decision(repo, seller_id, conversation_id, source_id, phase='reply', result_version=None, *, part_id=None):
    jobs = {j['job_id']: j for j in repo.list('job', conversation_id=conversation_id)}
    attempts = chain_attempts(repo, seller_id, conversation_id, source_id, phase, result_version, part_id=part_id)
    operation_jobs = {op.get('job_id') for op in repo.list('operation', conversation_id=conversation_id)}
    for job in jobs.values():
        if (not job.get('automatic') or job.get('kind') not in ('automatic_send', 'continuity_send')
                or job.get('source_message_id') != source_id or job.get('status') != 'failed'
                or job.get('job_id') in operation_jobs
                or part_id is not None and job.get('part_id') != part_id):
            continue
        plan = repo.get('continuity_plan', job.get('plan_id')) or {}
        if (plan.get('phase', 'reply') == phase
                and (phase != 'assistance' or plan.get('result_version') == result_version)
                and (phase != 'supplement' or set(plan.get('supplement_issue_ids', [])) & set(result_version or []))):
            return 'blocked', len(attempts), None
    if not attempts:
        return 'ready', 0, None
    last = attempts[-1]
    count = len(attempts)
    if last.get('status') != 'rejected' or last.get('submission_started') is not False:
        return 'blocked', count, None
    if count >= 3:
        return 'exhausted', count, None
    if last.get('error_code') not in TRANSIENT_CODES:
        return 'blocked', count, None
    return 'waiting', count, last.get('updated_at', 0) + BACKOFF_MS[count - 1]


def settle_waiting_jobs(repo, update_job, seller_id, conversation_id, source_id,
                        phase='reply', result_version=None, *, part_id=None):
    """End obsolete retry waits without changing any native attempt or error."""
    state, count, retry_at = decision(repo, seller_id, conversation_id, source_id, phase, result_version, part_id=part_id)
    if state not in ('blocked', 'exhausted'):
        return
    attempts = chain_attempts(repo, seller_id, conversation_id, source_id, phase, result_version, part_id=part_id)
    if not attempts:
        return
    last = attempts[-1]
    marker = state
    message = retry_message(state, count, retry_at, 0)
    if last.get('status') == 'accepted':
        marker, message = 'completed', '后续发送已受理，原失败记录保留'
    elif last.get('status') == 'unknown' or last.get('submission_started') is not False:
        marker, message = 'blocked', '原发送仍需核查，已停止重复自动尝试'
    for operation in attempts:
        job_id = operation.get('job_id')
        job = repo.get('job', job_id) if job_id else None
        if job and job.get('status') == 'deferred':
            update_job(job_id, status='cancelled', retry_at=None, retry_status=marker, message=message)


def restore_retry_job_states(repo, update_job, seller_id):
    """Project terminal legacy retry chains truthfully after an upgrade/restart."""
    chains = set()
    jobs = {job['job_id']: job for job in repo.list('job')}
    for operation in repo.list('operation'):
        if operation.get('seller_id') != seller_id or operation.get('kind') != 'send':
            continue
        job = jobs.get(operation.get('job_id'), {})
        if not job.get('automatic') or job.get('status') != 'deferred':
            continue
        cid, source = operation.get('conversation_id'), operation.get('source_message_id')
        if not cid or not source:
            continue
        phase = (operation.get('intent') or {}).get('continuity_phase', 'reply') if operation.get('purpose') == 'continuity' else 'reply'
        plan = repo.get('continuity_plan', operation.get('plan_id')) or {}
        version = (plan.get('result_version') if phase == 'assistance' else
                   tuple(plan.get('supplement_issue_ids', [])) if phase == 'supplement' else None)
        chains.add((cid, source, phase, version, operation.get('part_id')))
    for cid, source, phase, version, part_id in chains:
        settle_waiting_jobs(repo, update_job, seller_id, cid, source, phase, version, part_id=part_id)
