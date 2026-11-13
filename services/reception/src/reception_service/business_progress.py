"""Evidence-bound, conversation-local business follow-up projection.

Model output is advisory; only a matching accepted operation proves submission.
"""
from __future__ import annotations

import hashlib
import re

from .business_supplement import supplement_context, supplement_sources
from .repository import encoded, new_id, now_ms

_STATES = frozenset(('needs_input', 'needs_reply', 'waiting_customer', 'resolved'))
_LABELS = {'needs_input': '待你补充', 'needs_reply': '待回复', 'waiting_customer': '待客户回复',
           'idle': '暂无待办', 'pending_analysis': '状态待更新'}


def _signature(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def _topic(value):
    return re.sub(r'[\W_]+', '', value.casefold())


def _source_fingerprint(messages, note):
    return _signature([[m.get('message_id'), m.get('role'), m.get('text'), m.get('timestamp_ms'), m.get('attachments')] for m in messages] + [[note.get('revision'), note.get('body')]])


def _sources(repo, owner):
    cid = owner['conversation_id']
    messages = [m for m in repo.list('message', conversation_id=cid)
                if all(m.get(k) == owner.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id'))]
    messages.sort(key=lambda m: (m.get('timestamp_ms') if m.get('timestamp_ms') is not None else -1, m.get('message_id', '')))
    note = supplement_context(repo, cid)
    fingerprint = _source_fingerprint(messages, note)
    return messages, note, fingerprint


def build_progress_context(repo, owner, case=None):
    """Return compact existing issues and current note source for a normal AI call."""
    cid = owner['conversation_id']
    record = repo.get('business_progress', cid) or {}
    messages, note, _ = _sources(repo, owner)
    ids = {m.get('message_id') for m in messages}
    if note.get('source_id'):
        ids.add(note['source_id'])
    projected = project_progress(repo, owner)
    effective = {i['issue_id']: i for i in projected['issues']}
    operations = repo.list('operation', conversation_id=cid)
    completed_drafts = {o.get('draft_id') for o in operations if o.get('status') == 'accepted'
                        and o.get('seller_id') == owner.get('seller_id') and o.get('buyer_id') == owner.get('buyer_id')
                        and o.get('conversation_id') == cid and o.get('message_id') and o.get('submission_started') is True}
    issues = [dict(issue_id=i['issue_id'], topic=i['topic'],
                   summary=i['summary'] if all(x in ids for x in i.get('evidence_ids', [])) else '依据已变化，需要重新判断',
                   state=effective.get(i['issue_id'], i)['state'], evidence_ids=[x for x in i.get('evidence_ids', []) if x in ids])
              for i in record.get('issues', [])
              if i.get('state') != 'resolved'
              and not (i.get('reply_draft_id') in completed_drafts and i.get('reply_state') == 'resolved')
              and (case is None or i.get('case_id') == case.get('case_id'))]
    return {'issues': issues[:30], 'business_note_source_id': note.get('source_id')}


def apply_progress(repo, owner, case, context, raw, *, draft_id=None):
    """Apply independently validated issue candidates after the caller's source guard.

    Invalid/missing extension never affects reply generation or removes old issues.
    """
    cid = owner['conversation_id']
    if not isinstance(raw, dict) or not isinstance(raw.get('issues'), list) or len(raw['issues']) > 30:
        return
    messages, note, fingerprint = _sources(repo, owner)
    context_messages = (context.get('historical_evidence') or []) + (context.get('recent_messages') or context.get('messages') or [])
    current = {m.get('message_id'): m for m in messages}
    allowed = {m.get('message_id') for m in context_messages if isinstance(m, dict)
               and m.get('message_id') in current
               and all(m.get(k) == current[m['message_id']].get(k) for k in ('seller_id', 'buyer_id', 'conversation_id', 'role', 'text'))}
    if note.get('source_id') in supplement_sources(context):
        allowed.add(note['source_id'])
    record = repo.get('business_progress', cid) or {'issues': []}
    issues = record.get('issues', [])
    existing = {i.get('issue_id'): i for i in issues}
    previously_active = {i.get('issue_id') for i in issues if i.get('state') != 'resolved'}
    reviewed = set()
    accepted_any = False
    for row in raw['issues']:
        if not isinstance(row, dict):
            continue
        topic, summary, state, evidence = (row.get(k) for k in ('topic', 'summary', 'state', 'evidence_ids'))
        if (not isinstance(topic, str) or not topic.strip() or len(topic) > 100 or not _topic(topic)
                or not isinstance(summary, str) or not summary.strip() or len(summary) > 500
                or not isinstance(state, str) or state not in _STATES or not isinstance(evidence, list) or not evidence or len(evidence) > 30
                or any(not isinstance(x, str) or x not in allowed for x in evidence)):
            continue
        issue_id = row.get('issue_id')
        if issue_id is not None and (not isinstance(issue_id, str) or issue_id not in existing):
            continue
        issue = existing.get(issue_id) if issue_id else next((i for i in issues
            if i.get('case_id') == case.get('case_id') and _topic(i.get('topic', '')) == _topic(topic)), None)
        if issue and issue.get('case_id') != case.get('case_id'):
            continue
        if not issue:
            issue = {'issue_id': new_id('issue'), 'case_id': case.get('case_id')}
            issues.append(issue)
            existing[issue['issue_id']] = issue
        # A proposed reply is not a sent reply. Seller messages can independently
        # support a waiting state; a draft is promoted only by its own receipt.
        addresses = row.get('reply_addresses') is True and bool(draft_id)
        if state in ('waiting_customer', 'resolved') and addresses:
            state = 'needs_reply'
        elif state == 'waiting_customer' and not any(current.get(x, {}).get('role') == 'seller' for x in evidence):
            state = 'needs_reply'
        reviewed.add(issue['issue_id'])
        issue.update(topic=topic.strip(), summary=summary.strip(), state=state,
                     evidence_ids=list(dict.fromkeys(evidence)),
                     evidence_versions={x: _signature(current[x]) for x in evidence if x in current},
                     reply_draft_id=draft_id if addresses else None,
                     reply_state=row.get('state') if addresses else None)
        accepted_any = True
    if accepted_any or not raw['issues']:
        # A partial analysis must not certify omitted obligations against new
        # sources. Keep the old basis pending until each active issue is reviewed.
        basis = fingerprint if previously_active <= reviewed else record.get('source_fingerprint')
        record.update(seller_id=owner['seller_id'], buyer_id=owner['buyer_id'], conversation_id=cid,
                      issues=issues, source_fingerprint=basis, updated_at=now_ms())
        repo.put('business_progress', cid, record)


def project_progress(repo, owner, operations=None):
    cid = owner['conversation_id']
    record = repo.get('business_progress', cid)
    messages, note, fingerprint = _sources(repo, owner)
    operations = operations if operations is not None else repo.list('operation', conversation_id=cid)
    current = {m.get('message_id'): m for m in messages}
    active = []
    stale = not record or any(record.get(k) != owner.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id'))
    if record and not stale:
        draft_ids = {i.get('reply_draft_id') for i in record.get('issues', []) if i.get('reply_draft_id')}
        accepted_ids = {o.get('message_id') for o in operations if o.get('status') == 'accepted'
                        and o.get('draft_id') in draft_ids and o.get('seller_id') == owner.get('seller_id')
                        and o.get('buyer_id') == owner.get('buyer_id') and o.get('conversation_id') == cid
                        and o.get('message_id') and o.get('submission_started') is True}
        stale = (record.get('source_fingerprint') != fingerprint and record.get('source_fingerprint') !=
                 _source_fingerprint([m for m in messages if m.get('message_id') not in accepted_ids], note))
        for original in record.get('issues', []):
            if original.get('state') == 'resolved':
                continue
            issue = dict(original)
            if any(mid not in current or _signature(current[mid]) != version for mid, version in issue.get('evidence_versions', {}).items()):
                stale = True
                continue
            if any(x.startswith('business_note_') and x != note.get('source_id') for x in issue.get('evidence_ids', [])):
                stale = True
                continue
            if issue.get('reply_draft_id') and any(o.get('status') == 'accepted'
                    and o.get('draft_id') == issue['reply_draft_id'] and o.get('seller_id') == owner.get('seller_id')
                    and o.get('buyer_id') == owner.get('buyer_id') and o.get('conversation_id') == cid
                    and o.get('message_id') and o.get('submission_started') is True for o in operations):
                issue['state'] = issue.get('reply_state') if issue.get('reply_state') in ('waiting_customer', 'resolved') else 'waiting_customer'
            if issue['state'] != 'resolved':
                active.append({k: issue[k] for k in ('issue_id', 'topic', 'summary', 'state', 'evidence_ids')})
    pending_receipt = any(o.get('status') in ('prepared', 'sending', 'unknown') for o in operations)
    chosen = next((i for state in ('needs_input', 'needs_reply', 'waiting_customer') for i in active if i['state'] == state), None)
    state = 'pending_analysis' if stale else chosen['state'] if chosen else 'idle'
    if pending_receipt and state == 'waiting_customer':
        state = 'needs_reply'
    reason = ('来源已变化，等待正常接待分析' if stale else '发送结果尚待确认' if pending_receipt and state == 'needs_reply'
              else chosen['summary'] if chosen else '当前没有已识别的待办')
    label = _LABELS[state] + (('：' + chosen['topic']) if state == 'needs_input' and chosen else '')
    return {'state': state, 'label': label, 'reason': reason, 'issues': active,
            'updated_at': record.get('updated_at') if record else None}
