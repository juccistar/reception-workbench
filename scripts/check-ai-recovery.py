"""One explicitly authorized synthetic AI attempt; never a customer/send workflow."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services' / 'reception' / 'src'))

from reception_service.context import validate_reply
from reception_service.errors import ServiceError
from reception_service.integrations.analysis import prepare_context, validate_analysis
from reception_service.integrations.live import AI_INSTRUCTIONS, LiveIntegration, unique_object
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
from reception_service.integrations.accio_native.vendor.adapters.desktop_query import DesktopQueryRunner, _OwnRecords
from reception_service.integrations.accio_native.vendor.adapters.tool_signals import output_has_tools
from reception_service.integrations.accio_native.vendor.diagnostics import CODES, STAGES, process_diagnostics
from reception_service.synthetic_check import synthetic_context

REQUIRED = ('ACCIO_GATEWAY_TOKEN', 'ACCIO_NODE_BIN', 'ACCIO_MCP_SCRIPT')


GENERATIONS = Path('plugins/data/cli-tools/plugins/alibaba-com-seller-assistant/tools/workctl/generations')
EXE_RELATIVE = Path('node_modules/@accio-ai/cli/node_modules/@accio-ai/cli-win32-x64/workctl.exe')


def absolute_path(file):
    value = os.path.abspath(file)
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)


def filesystem_path(file):
    """Use Windows extended paths for deep public generations, without aliases."""
    absolute = absolute_path(file)
    value = str(absolute)
    if os.name == 'nt' and len(value) >= 248:
        return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return absolute


def account_root(file):
    if not file:
        return None
    return next((parent for parent in absolute_path(file).parents
                 if parent.parent.name.lower() == 'accounts'), None)


def safe_file(file):
    absolute = absolute_path(file)
    access = filesystem_path(absolute)
    if not access.is_file():
        raise ValueError('workctl_invalid')
    # Reject aliases even inside the same account; never infer a binding from a
    # symlink/junction's destination or follow one out of the supplied directory.
    if absolute_path(access.resolve(strict=True)) != absolute:
        raise ValueError('runtime_path_unsafe')
    return absolute


def public_workctl(file):
    absolute = safe_file(file)
    if absolute.name.lower() == 'workctl.exe':
        return absolute
    account = account_root(absolute)
    parts = absolute.relative_to(account).parts if account else ()
    prefix = GENERATIONS.parts
    if (len(parts) != len(prefix) + 4
            or Path(*parts[:len(prefix)]) != GENERATIONS
            or Path(parts[-2]) != Path('prefix')
            or parts[-1].lower() not in ('workctl', 'workctl.cmd', 'workctl.ps1')):
        raise ValueError('workctl_invalid')
    return safe_file(absolute.parent / EXE_RELATIVE)


def inspect_runtime(cli, environment):
    """Independently rebuild the host/MCP binding; never trust Node's selection."""
    mcp = environment.get('ACCIO_MCP_SCRIPT', '')
    host = environment.get('PHOENIX_PLUGIN_CLI_WORKCTL_PATH', '').strip()
    mcp_account, host_account = account_root(mcp), account_root(host)
    report = {'mcp_account_root_found': mcp_account is not None,
              'host_workctl_supplied': bool(host), 'host_workctl_account_root_found': host_account is not None,
              'account_binding_source': 'none', 'account_binding_found': False,
              'workctl_account_root_found': False, 'account_roots_match': None,
              'runtime_ready': False, 'scope_reason': 'runtime_scope_invalid'}
    try:
        safe_file(mcp)
        if host:
            if not Path(host).is_absolute():
                raise ValueError('host_workctl_invalid')
            try:
                host_cli = public_workctl(host)
            except ValueError as error:
                raise ValueError('runtime_path_unsafe' if str(error) == 'runtime_path_unsafe'
                                 else 'host_workctl_invalid') from None
            host_account = account_root(host_cli)
        if host and not host_account:
            raise ValueError('account_directory_mismatch')
        if mcp_account and host_account and mcp_account != host_account:
            raise ValueError('account_directory_mismatch')
        supplied = mcp_account or host_account
        report['account_binding_source'] = ('mcp_and_host_workctl' if mcp_account and host_account else
                                            'mcp' if mcp_account else 'host_workctl' if host_account else 'none')
        report['account_binding_found'] = supplied is not None
        executable = safe_file(cli)
        if executable.name.lower() != 'workctl.exe':
            raise ValueError('workctl_invalid')
        cli_account = account_root(executable)
        report['workctl_account_root_found'] = cli_account is not None
        report['account_roots_match'] = cli_account == supplied if supplied and cli_account else None
        report['scope_reason'] = ('account_binding_missing' if not supplied else
                                  'workctl_account_root_missing' if not cli_account else
                                  'account_directory_mismatch' if cli_account != supplied else
                                  'runtime_paths_consistent')
        report['runtime_ready'] = report['scope_reason'] == 'runtime_paths_consistent'
    except ValueError as error:
        report['scope_reason'] = str(error) if str(error) in {
            'workctl_invalid', 'host_workctl_invalid', 'runtime_path_unsafe', 'account_directory_mismatch'
        } else 'runtime_path_unsafe'
    except (OSError, RuntimeError):
        report['scope_reason'] = 'runtime_path_unsafe'
    return report


def valid_runtime(cli, environment):
    return (all(environment.get(key, '').strip() for key in REQUIRED)
            and filesystem_path(environment['ACCIO_NODE_BIN']).is_file()
            and inspect_runtime(cli, environment)['runtime_ready'])


def validate_result(result, context):
    if not isinstance(result, dict) or output_has_tools(result):
        raise ServiceError('tool_approval', 'Synthetic result rejected', stage='answer')
    if result.get('success') is False or result.get('ok') is False or result.get('error'):
        raise ServiceError('native_error', 'Synthetic result unavailable', stage='answer')
    data = result.get('data') if isinstance(result.get('data'), dict) else result
    answer = next((data[key] for key in ('answer', 'finalAnswer', 'final_answer') if data.get(key) is not None), None)
    if isinstance(answer, str):
        answer = answer.strip()
        if answer.startswith('```json') and answer.endswith('```'):
            answer = answer[7:-3].strip()
        try:
            answer = json.loads(answer, object_pairs_hook=unique_object)
        except (ValueError, TypeError, RecursionError):
            raise ServiceError('answer_non_json', 'Synthetic result unavailable', stage='answer') from None
    if not isinstance(answer, dict):
        raise ServiceError('answer_missing', 'Synthetic result unavailable', stage='answer')
    analysis = validate_analysis(answer, context)
    if not validate_reply(analysis.get('reply', ''), analysis.get('reply_sources', []), context)['ready']:
        raise ServiceError('analysis_invalid', 'Synthetic validation blocked', stage='analysis')


def probe(root, cli, seller, *, environment=None, facade_factory=LiveIntegration,
          runner_factory=DesktopQueryRunner, reader_factory=_OwnRecords):
    environment = os.environ if environment is None else environment
    report = {'purpose': 'single_synthetic_recovery_probe', 'status': 'blocked', 'code': 'not_started',
              'stage': 'runtime_preflight', 'seller_identity_checked': False, 'seller_identity_verified': False,
              'seller_suffix': seller[-6:] if isinstance(seller, str) else '', 'ai_attempts': 0,
              'customer_messages_sent': 0, 'feishu_writes': 0, 'response_validated': False, 'recovery_validated': False,
              'diagnostics': {}, 'observed_at': datetime.now(timezone.utc).isoformat()}
    if not isinstance(seller, str) or not re.fullmatch(r'[1-9][0-9]*', seller):
        report['code'] = 'seller_invalid'
        return report
    if any(not environment.get(key, '').strip() for key in REQUIRED):
        report['code'] = 'authorized_environment_missing'
        return report
    if not all(filesystem_path(environment[key]).is_file() for key in ('ACCIO_NODE_BIN', 'ACCIO_MCP_SCRIPT')):
        report['code'] = 'runtime_component_missing'
        return report
    report['runtime_diagnostics'] = inspect_runtime(cli, environment)
    if not report['runtime_diagnostics']['runtime_ready']:
        report['code'] = 'runtime_scope_invalid'
        return report
    cli = filesystem_path(cli)
    output = Path(root) / 'artifacts' / 'verification' / 'ai-recovery-pilot'
    output.mkdir(parents=True, exist_ok=True)
    claim = output / 'attempt.claim.json'
    probe_id = 'synthetic-recovery-' + uuid.uuid4().hex
    try:
        with claim.open('x', encoding='utf-8') as stream:
            json.dump({'probe_id': probe_id, 'seller_suffix': seller[-6:],
                       'purpose': report['purpose'], 'started_at': report['observed_at']}, stream)
    except FileExistsError:
        report['code'] = 'attempt_already_claimed'
        return report  # Never overwrite an earlier attempt or authorize another call.
    runner = None
    started = time.monotonic()
    try:
        settings = {'workctl_path': str(cli), 'feishu': {'enabled': False, 'write_enabled': False}}
        facade = facade_factory(output / 'isolated-runtime', settings)
        report['stage'] = 'identity'
        report['seller_identity_checked'] = True
        resolved = facade.resolve_identity(seller)
        report['seller_identity_verified'] = resolved == seller
        if resolved != seller:
            raise ServiceError('identity_invalid', 'Synthetic seller mismatch', stage='identity')
        context, _, _ = prepare_context(synthetic_context(resolved))
        report['stage'] = 'agent_selection'
        agent_args = facade._agent_args()
        if len(agent_args) != 2 or agent_args[0] != '--agent-id':
            raise ServiceError('agent_missing', 'Synthetic agent unavailable', stage='agent_selection')
        # Check recovery availability before consuming the one AI authorization.
        reader = reader_factory(str(cli), 'reception-draft-preflight', agent_args[1],
                                'SYNTHETIC PREFLIGHT', time.time(), time.monotonic() + 190)
        if reader.folder is None or not reader.folder.is_dir():
            report['code'] = 'recovery_unavailable'
            return report
        runner = runner_factory(facade.native.data_dir, process=facade.native.process)
        report['stage'] = 'cli'
        report['ai_attempts'] = 1
        result = runner.run([str(cli), 'desktop', 'query', *agent_args, '--input', '-', '--json',
                             '--request-timeout', '180s'],
                            input=AI_INSTRUCTIONS + json.dumps(context, ensure_ascii=False, allow_nan=False),
                            timeout=190, recover_own_results=True)
        report['stage'] = 'analysis'
        validate_result(result, context)
        report.update(status='passed', code='ok', stage='complete', response_validated=True)
    except (ServiceError, AdapterError) as error:
        code = getattr(error, 'code', None)
        stage = getattr(error, 'stage', None)
        report.update(status='failed', code=code if code in CODES else 'native_error',
                      stage=stage if stage in STAGES else report.get('stage', 'cli'))
        report['diagnostics'].update(process_diagnostics(getattr(error, 'diagnostics', {})))
    except Exception:
        report.update(status='failed', code='native_error')  # No raw text/traceback.
    finally:
        if runner is not None:
            report['diagnostics'].update(process_diagnostics(runner.last_diagnostics))
        report['recovery_validated'] = (report['response_validated'] is True
                                        and report['diagnostics'].get('result_source') == 'completed_record')
        report['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
        report['probe_id'] = probe_id
        temporary = output / 'result.json.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
        temporary.replace(output / 'result.json')
    return report


def main():
    parser = argparse.ArgumentParser(description='One synthetic AI recovery attempt, no sends or writes')
    parser.add_argument('--workctl', type=Path, required=True)
    parser.add_argument('--expect-seller', required=True)
    args = parser.parse_args()
    report = probe(ROOT, args.workctl, args.expect_seller)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
