"""Read-only snapshot of each conversation's automatic reception state."""
from collections import defaultdict

from .repository import now_ms
from .errors import ServiceError


class ReceptionProjection:
    def __init__(self, service):
        self.service = service
        self.stamp = now_ms()
        self.cases = {c['case_id']: c for c in service.repo.list('case')}
        self.jobs = defaultdict(list)
        self.drafts = defaultdict(list)
        self.operations = defaultdict(list)
        jobs = service.repo.list('job')
        for job in jobs:
            self.jobs[job.get('conversation_id')].append(job)
        for draft in service.repo.list('draft'):
            self.drafts[draft.get('conversation_id')].append(draft)
        for operation in service.repo.list('operation'):
            self.operations[operation.get('conversation_id')].append(operation)
        self.reserved_ai = sum(j.get('ai_reserved_remaining', int(not j.get('external_started')))
                               for j in jobs if j.get('kind') == 'generate_draft' and j.get('automatic')
                               and j.get('status') in ('queued', 'running'))
        # The send budget is charged when its operation is created, before the
        # native call. Index operations once, including completed receipts whose
        # job has not yet transitioned out of running.
        send_operations = {}
        sending_cases = set()
        for operations in self.operations.values():
            for operation in operations:
                if operation.get('draft_id'):
                    key = (operation['draft_id'], operation.get('conversation_id'), operation.get('case_id'))
                    send_operations[key] = max(send_operations.get(key, 0), operation.get('updated_at') or 0)
                if operation.get('status') == 'sending':
                    sending_cases.add((operation.get('conversation_id'), operation.get('case_id')))

        def send_charged(job):
            if job.get('status') != 'running':
                return False
            if job.get('draft_id'):
                key = (job['draft_id'], job.get('conversation_id'), job.get('case_id'))
                return key in send_operations and send_operations[key] >= (job.get('started_at') or 0)
            return (job.get('conversation_id'), job.get('case_id')) in sending_cases

        self.reserved_send = sum(j.get('kind') == 'automatic_send' and
                                 j.get('status') in ('queued', 'running') and not send_charged(j)
                                 for j in jobs)
        self.budget = service._budget()

    def reply_pending(self, conversation):
        cid = conversation['conversation_id']
        messages = self.service._messages(cid)
        buyer = [m for m in messages if m.get('role') == 'buyer']
        seller = [m for m in messages if m.get('role') == 'seller']
        unanswered = bool(buyer) and (any(m.get('timestamp_ms') is None for m in buyer)
                     or not seller or seller[-1].get('timestamp_ms') is None
                     or any(m.get('timestamp_ms') is not None and m['timestamp_ms'] > seller[-1]['timestamp_ms'] for m in buyer))
        if buyer and any(o.get('status') == 'accepted' and o.get('source_message_id') == buyer[-1]['message_id']
                         for o in self.operations[cid]):
            unanswered = False
        return bool(unanswered)

    def status(self, conversation):
        s = self.service
        cid = conversation['conversation_id']
        case = self.cases.get(conversation.get('case_id'))
        jobs, drafts, operations = self.jobs[cid], self.drafts[cid], self.operations[cid]
        active = next((j for j in jobs if j.get('status') in ('queued', 'running')
                       and j.get('kind') in ('generate_draft', 'automatic_send', 'send_chat', 'confirm_send', 'continuity_send')), None)
        activity = ({'kind': active['kind'], 'status': active['status'], 'job_id': active['job_id']}
                    if active else None)
        blockers = []

        def block(code, message):
            blockers.append({'code': code, 'message': message})

        if any(o.get('status') == 'unknown' for o in operations):
            block('unknown', '发送结果未知，请先核对')
        if any(o.get('status') in ('prepared', 'sending') for o in operations):
            block('send_pending', '本会话有待确认或发送中的操作')
        human = conversation.get('human_owned') or case and case.get('state') == 'human_owned'
        if human:
            block('human_owned', '客户已由人工接管')
        case_valid = bool(case and case.get('seller_id') == s.seller_id and
                          case.get('buyer_id') == conversation.get('buyer_id') and case.get('conversation_id') == cid)
        if conversation.get('case_id') is not None and not case_valid:
            block('case', '当前客户事项或归属不可用')
        elif case and case.get('state') == 'closed':
            block('closed', '客户会话已关闭')
        if not s._running or s._manual_only:
            block('paused', '自动接待已暂停' if not s._running else '当前为仅人工模式')
        scope = s._frozen_scope
        in_scope = bool(scope and scope.get('seller_id') == s.seller_id and
                        (scope.get('mode') == 'incoming' or cid in scope.get('conversation_ids', [])))
        if s._running and not s._manual_only and not in_scope:
            block('out_of_scope', '客户不在本次自动接待范围')
        if not s.account_verified or s.mode == 'live' and s.checks['messages']['status'] != 'passed':
            block('capability', '消息读取能力尚未就绪')
        if s.mode == 'live':
            try:
                s._freeze_reception_model()
                ready = s.checks['ai']['status'] == 'passed'
            except ServiceError:
                ready = False
            if not ready:
                block('ai_capability', 'AI 接待能力尚未就绪')
        # Send previews are diagnostic, not a gate: each send performs its own
        # native preflight, so even a failed earlier preview is not a blocker.
        if s._message_sync_pending(cid):
            block('body_pending', '正在等待消息正文同步')
        source = s._automatic_source(cid, operations=operations) if case else None
        if source and conversation.get('last_buyer_at') and max(0, self.stamp - conversation['last_buyer_at']) < s.settings['merge_seconds'] * 1000:
            block('merge', '等待合并客户连续消息')
        if not activity and source:
            retry = next((j for j in sorted(jobs, key=lambda j: j['created_at'], reverse=True)
                          if j.get('status') == 'deferred' and j.get('retry_at')
                          and j.get('source_message_id') == source['message_id']
                          and j.get('kind') in ('generate_draft', 'automatic_send', 'continuity_send')
                          and j.get('epoch', s._epoch) == s._epoch), None)
            if retry:
                active = retry
                activity = {'kind': retry['kind'], 'status': 'deferred', 'job_id': retry['job_id']}
        pending = any(d.get('case_id') == conversation.get('case_id') and
                      (d.get('status') in ('pending', 'sending', 'unknown') or
                       d.get('origin') == 'manual' and d.get('status') == 'stale') for d in drafts)
        if pending and not activity:
            block('draft_review', '已有草稿等待人工处理' if not s._auto_send else '已有草稿等待处理')
        if blockers:
            state, reason = 'blocked', blockers[0]['message']
        elif activity:
            state, reason = 'active', ('正在等待自动重试，接待继续' if active['status'] == 'deferred' else
                                       '自动接待任务正在处理' if active.get('automatic') else '客户任务正在处理')
        elif not source or not conversation.get('last_buyer_at'):
            state, reason = 'waiting', '等待新的客户活动'
        elif (s._generation_attempted(cid, case, source, jobs=jobs, drafts=drafts) or
              not s._post_quote_analysis_allowed(case, source, jobs=jobs, drafts=drafts)):
            state, reason = 'handled', '当前消息已处理，等待新活动'
        else:
            state, reason = 'ready', '等待自动接待任务'
        owner = 'human' if human else ('automatic' if s._running and not s._manual_only and in_scope and case_valid and case.get('state') != 'closed' else 'none')
        return {'state': state, 'reason': reason, 'owner': owner, 'activity': activity,
                'blockers': blockers, 'observed_at': self.stamp}
