"""Account-scoped agent metadata and fixed, content-free AI diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .common import AdapterError


AGENT_FIELDS = ('agentType', 'id', 'agentId')


def desktop_agent_args(cli):
    """Use only metadata belonging to the account owning this exact CLI."""
    try:
        account = next((p for p in Path(cli).resolve().parents if p.parent.name == 'accounts'), None)
        if account is None:
            return []  # Nonstandard installations retain native CLI discovery.
        agents = account / 'agents'
        if not agents.exists():
            raise AdapterError('当前账号未安装国际站助手', code='agent_missing', stage='agent_selection')
        if not agents.resolve().is_relative_to(account):
            raise ValueError
        matches = []
        for folder in agents.iterdir():
            if not folder.is_dir():
                continue
            config = folder / 'agent-core' / 'agent.json'
            if not config.resolve().is_relative_to(account):
                raise ValueError
            try:
                config.stat()
            except FileNotFoundError:
                # Accio also stores non-agent directories here. Only published
                # agent-core/agent.json files participate in native selection.
                continue
            # Discard all non-metadata fields during decoding; never inspect
            # prompts, sessions, model settings or authorization material.
            data = json.loads(config.read_text(encoding='utf-8-sig'),
                object_hook=lambda row: {key: row[key] for key in AGENT_FIELDS if key in row})
            if not isinstance(data, dict) or not isinstance(data.get('agentType'), str) or not data['agentType']:
                raise ValueError
            if data['agentType'] != 'alibaba-com-seller-assistant':
                continue
            identifiers = [data[key] for key in ('agentId', 'id') if key in data]
            if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', value)
                   for value in identifiers) or len(set(identifiers)) > 1:
                raise ValueError
            agent_id = identifiers[0] if identifiers else folder.name
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', agent_id):
                raise ValueError
            matches.append(agent_id)
    except AdapterError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise AdapterError('当前账号助手元数据无效或不可读取',
                           code='agent_metadata_invalid', stage='agent_selection') from None
    if not matches:
        raise AdapterError('当前账号未安装国际站助手', code='agent_missing', stage='agent_selection')
    if len(matches) != 1:
        raise AdapterError('当前账号存在多个国际站助手，无法唯一选择',
                           code='agent_ambiguous', stage='agent_selection')
    return ['--agent-id', matches[0]]


def staged_error(exc, stage, default_code):
    """Keep machine diagnostics, replacing any exception message with fixed text."""
    code = exc.code if isinstance(exc, AdapterError) and exc.code != 'adapter_error' else default_code
    exit_code = exc.exit_code if isinstance(exc, AdapterError) else None
    diagnostics = exc.diagnostics if isinstance(exc, AdapterError) else None
    return AdapterError('Accio AI 校验失败（%s/%s）' % (stage, code),
                        code=code, stage=stage, exit_code=exit_code, diagnostics=diagnostics)
