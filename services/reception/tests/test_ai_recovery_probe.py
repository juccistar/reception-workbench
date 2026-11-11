"""Synthetic pilot, exclusive call claim and exact-record matching. No real AI."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
from reception_service.integrations.accio_native.vendor.adapters.desktop_query import _OwnRecords

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('synthetic_recovery_probe', ROOT / 'scripts/check-ai-recovery.py')
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def answer():
    return {'action': 'ask', 'facts': {'product': {'value': 'ceramic mugs', 'evidence_ids': ['synthetic-ai-check-message']}},
            'question_keys': ['quantity'], 'reply': 'Could you share the quantity you need?',
            'summary': 'Synthetic mug request', 'missing': ['quantity'], 'conflicts': [],
            'procurement_key': 'synthetic-ai-check', 'new_case': False, 'reply_sources': ['synthetic-ai-check-message']}


class RecoveryProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ai-recovery-probe-'))
        # Cleanup must also support paths beyond MAX_PATH on this Windows Python.
        cleanup_root = Path('\\\\?\\' + str(self.root)) if os.name == 'nt' else self.root
        self.addCleanup(shutil.rmtree, cleanup_root)
        account = self.root / 'accounts' / 'synthetic-account'
        account.mkdir(parents=True)
        self.folder = account / 'conversations/dm'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cli = account / 'workctl.exe'
        self.mcp = account / 'mcp.js'
        self.node = self.root / 'node.exe'
        for file in (self.cli, self.mcp, self.node):
            file.touch()
        self.env = {'ACCIO_GATEWAY_TOKEN': 'SYNTHETIC-NON-CREDENTIAL',
                    'ACCIO_NODE_BIN': str(self.node), 'ACCIO_MCP_SCRIPT': str(self.mcp)}
        self.calls = []
        outer = self
        class Runner:
            last_diagnostics = {'result_source': 'completed_record', 'recovery_enabled': True,
                                'recovery_status': 'completed', 'cli_exit_code': -1, 'cli_terminated': True}
            def __init__(self, *_args, **_kwargs):
                pass
            def run(self, args, **kwargs):
                outer.calls.append((args, kwargs))
                return {'answer': json.dumps(answer())}
        self.runner = Runner
        self.facade = SimpleNamespace(resolve_identity=Mock(return_value='10001'),
            _agent_args=Mock(return_value=['--agent-id', 'synthetic-agent']),
            native=SimpleNamespace(data_dir=self.root / 'isolated', process=object()))
        self.factory = Mock(return_value=self.facade)

    def run_probe(self, **kwargs):
        options = dict(environment=self.env, facade_factory=self.factory, runner_factory=self.runner)
        options.update(kwargs)
        return pilot.probe(self.root, self.cli, '10001', **options)

    def test_matching_recovered_answer_is_validated_without_exporting_body(self):
        result = self.run_probe()
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['response_validated'])
        self.assertTrue(result['recovery_validated'])
        self.assertTrue(result['seller_identity_checked'])
        self.assertTrue(result['seller_identity_verified'])
        self.assertEqual(result['diagnostics']['result_source'], 'completed_record')
        self.assertEqual(result['ai_attempts'], 1)
        args, kwargs = self.calls[0]
        self.assertEqual(args[-2:], ['--request-timeout', '180s'])
        self.assertEqual(kwargs['timeout'], 190)
        self.assertTrue(kwargs['recover_own_results'])
        self.assertIn('SYNTHETIC TEST ONLY', kwargs['input'])
        self.assertNotIn('--approve-tools', args)
        stored = (self.root / 'artifacts/verification/ai-recovery-pilot/result.json').read_text()
        self.assertNotIn('Could you share', stored)
        self.assertNotIn(self.env['ACCIO_GATEWAY_TOKEN'], stored)
        self.assertEqual(result['customer_messages_sent'], 0)
        self.assertEqual(result['feishu_writes'], 0)

    def test_stdout_success_does_not_falsely_claim_recovery_was_used(self):
        runner = Mock(last_diagnostics={'result_source': 'stdout', 'recovery_enabled': True})
        runner.run.return_value = {'answer': json.dumps(answer())}
        result = self.run_probe(runner_factory=lambda *_args, **_kwargs: runner)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['response_validated'])
        self.assertFalse(result['recovery_validated'])

    def test_no_authorization_never_claims_or_invokes_facade(self):
        result = self.run_probe(environment={})
        self.assertEqual(result['code'], 'authorized_environment_missing')
        self.assertFalse((self.root / 'artifacts').exists())
        self.factory.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_other_account_cli_is_rejected_before_claim(self):
        foreign = self.root / 'accounts/other/workctl.exe'
        foreign.parent.mkdir()
        foreign.touch()
        result = pilot.probe(self.root, foreign, '10001', environment=self.env, facade_factory=self.factory)
        self.assertEqual(result['code'], 'runtime_scope_invalid')
        self.assertFalse(result['seller_identity_checked'])
        self.factory.assert_not_called()

    def test_duplicate_and_concurrent_start_cannot_generate_again(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.run_probe(), range(4)))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(row['status'] == 'passed' for row in results), 1)
        self.assertEqual(sum(row['code'] == 'attempt_already_claimed' for row in results), 3)
        self.assertEqual(self.run_probe()['code'], 'attempt_already_claimed')
        self.assertEqual(len(self.calls), 1)

    def test_seller_mismatch_does_not_generate(self):
        self.facade.resolve_identity.return_value = '99999'
        result = self.run_probe()
        self.assertEqual(result['code'], 'identity_invalid')
        self.assertEqual(result['ai_attempts'], 0)
        self.assertEqual(self.calls, [])

    def test_unavailable_recovery_does_not_consume_ai(self):
        result = self.run_probe(reader_factory=lambda *_: SimpleNamespace(folder=None))
        self.assertEqual(result['code'], 'recovery_unavailable')
        self.assertEqual(self.calls, [])

    def test_native_failure_stays_failed_and_does_not_export_raw_error(self):
        runner = Mock()
        runner.last_diagnostics = {'result_source': 'stdout', 'raw': 'SYNTHETIC-PRIVATE'}
        runner.run.side_effect = AdapterError('SYNTHETIC-PRIVATE', code='cli_unclassified_error', stage='cli')
        result = self.run_probe(runner_factory=lambda *_args, **_kwargs: runner)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'cli_unclassified_error')
        self.assertEqual(runner.run.call_count, 1)
        self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(result))

    def test_non_json_reply_is_not_a_success(self):
        runner = Mock(last_diagnostics={})
        runner.run.return_value = {'answer': 'SYNTHETIC-NON-JSON'}
        result = self.run_probe(runner_factory=lambda *_args, **_kwargs: runner)
        self.assertEqual(result['code'], 'answer_non_json')
        self.assertFalse(result['response_validated'])

    def global_host(self, name='workctl.cmd'):
        # Reproduce the A01 layout using only synthetic files. The Accio directory
        # account deliberately differs from the native seller AliID (10001).
        account = self.root / 'accounts/7100188443'
        prefix = account / pilot.GENERATIONS / '0.1.64/mvuec994-57222bb83950/prefix'
        self.cli = prefix / pilot.EXE_RELATIVE
        shim = prefix / name
        self.mcp = self.root / 'Program Files (x86)/Accio/resources/accio-mcp-cli/accio-mcp.mjs'
        for file in (self.cli, shim, self.mcp):
            access = pilot.filesystem_path(file)
            access.parent.mkdir(parents=True, exist_ok=True)
            access.write_text('SYNTHETIC PUBLIC FILE - NEVER EXECUTE', encoding='utf-8')
        self.folder = account / 'conversations/dm'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.env.update(ACCIO_MCP_SCRIPT=str(self.mcp), PHOENIX_PLUGIN_CLI_WORKCTL_PATH=str(shim))
        return shim

    def assert_scope_blocked(self, result, reason):
        self.assertEqual(result['code'], 'runtime_scope_invalid', result)
        self.assertEqual(result['runtime_diagnostics']['scope_reason'], reason)
        self.assertEqual(result['ai_attempts'], 0)
        self.assertFalse(result['seller_identity_checked'])
        self.assertFalse((self.root / 'artifacts').exists())
        self.factory.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn(self.env['ACCIO_GATEWAY_TOKEN'], json.dumps(result))

    def junction(self, target, alias):
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(alias))
        else:
            alias.symlink_to(target, target_is_directory=True)

    def test_global_mcp_host_shim_is_independently_bound_and_native_seller_is_verified(self):
        self.global_host()
        result = self.run_probe()
        self.assertEqual(result['status'], 'passed', result)
        runtime = result['runtime_diagnostics']
        self.assertFalse(runtime['mcp_account_root_found'])
        self.assertTrue(runtime['host_workctl_account_root_found'])
        self.assertEqual(runtime['account_binding_source'], 'host_workctl')
        self.assertTrue(runtime['account_roots_match'])
        self.facade.resolve_identity.assert_called_once_with('10001')
        self.assertEqual(self.calls[0][0][0], str(pilot.filesystem_path(self.cli)))
        self.assertTrue((self.root / 'artifacts/verification/ai-recovery-pilot/attempt.claim.json').is_file())
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn(self.env['ACCIO_GATEWAY_TOKEN'], json.dumps(result))

    def test_matching_mcp_and_host_sources_are_both_recorded(self):
        self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH'] = str(self.cli)
        result = self.run_probe()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['runtime_diagnostics']['account_binding_source'], 'mcp_and_host_workctl')

    def test_host_exe_and_known_shim_variants_use_the_same_binding(self):
        for name in ('workctl.cmd', 'workctl.ps1', 'workctl'):
            with self.subTest(name=name):
                # Validate only: this loop must not bypass the single-attempt claim.
                self.global_host(name)
                self.assertTrue(pilot.valid_runtime(self.cli, self.env))
        self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH'] = str(self.cli)
        self.assertTrue(pilot.valid_runtime(self.cli, self.env))
        self.assertFalse((self.root / 'artifacts').exists())

    def test_global_mcp_without_host_rejects_an_explicit_account_exe(self):
        self.global_host()
        del self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH']
        self.assert_scope_blocked(self.run_probe(), 'account_binding_missing')

    def test_global_mcp_path_shim_does_not_substitute_for_host_binding(self):
        shim = self.global_host()
        self.env['PATH'] = str(shim.parent)
        del self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH']
        self.assert_scope_blocked(self.run_probe(), 'account_binding_missing')

    def test_unowned_host_rejected_before_identity_probe_even_with_valid_cli(self):
        host = self.root / 'shared' / 'workctl.exe'
        host.parent.mkdir()
        host.touch()
        self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH'] = str(host)
        self.assert_scope_blocked(self.run_probe(), 'account_directory_mismatch')
        self.facade.resolve_identity.assert_not_called()

    def test_mcp_host_account_conflict_rejects_even_a_cli_matching_host(self):
        original_mcp = self.mcp
        self.global_host()
        self.env['ACCIO_MCP_SCRIPT'] = str(original_mcp)
        self.assert_scope_blocked(self.run_probe(), 'account_directory_mismatch')

    def test_host_binding_rejects_a_different_final_exe_account(self):
        original_cli = self.cli
        self.global_host()
        self.cli = original_cli
        self.assert_scope_blocked(self.run_probe(), 'account_directory_mismatch')

    def test_arbitrary_host_shim_cannot_authorize_an_otherwise_valid_cli(self):
        self.global_host()
        shim = self.root / 'accounts/7100188443/untrusted/workctl.cmd'
        shim.parent.mkdir()
        shim.write_text('SYNTHETIC SHIM - NEVER READ OR EXECUTE')
        self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH'] = str(shim)
        self.assert_scope_blocked(self.run_probe(), 'host_workctl_invalid')

    def test_host_shim_requires_the_fixed_exe_even_when_another_account_tool_exists(self):
        original_cli = self.cli
        self.global_host()
        pilot.filesystem_path(self.cli).unlink()
        self.cli = original_cli
        self.assert_scope_blocked(self.run_probe(), 'host_workctl_invalid')

    def test_host_binding_still_checks_native_seller_and_retains_claim_on_mismatch(self):
        self.global_host()
        self.facade.resolve_identity.return_value = '99999'
        result = self.run_probe()
        self.assertEqual(result['code'], 'identity_invalid')
        self.assertEqual(self.run_probe()['code'], 'attempt_already_claimed')
        self.assertEqual(self.calls, [])

    def test_cross_account_junction_in_final_exe_is_rejected_before_claim(self):
        target = self.root / 'accounts/other/bin'
        target.mkdir(parents=True)
        (target / 'workctl.exe').touch()
        alias = self.cli.parent / 'redirect'
        self.junction(target, alias)
        self.cli = alias / 'workctl.exe'
        self.assert_scope_blocked(self.run_probe(), 'runtime_path_unsafe')

    def test_mcp_junction_cannot_supply_a_trusted_account(self):
        target = self.root / 'outside-runtime'
        target.mkdir()
        (target / 'mcp.mjs').touch()
        alias = self.cli.parent / 'runtime'
        self.junction(target, alias)
        self.env['ACCIO_MCP_SCRIPT'] = str(alias / 'mcp.mjs')
        self.assert_scope_blocked(self.run_probe(), 'runtime_path_unsafe')

    def test_host_shim_junction_cannot_bind_an_external_prefix(self):
        shim = self.global_host()
        target = self.root / 'external-prefix'
        target.mkdir()
        (target / 'workctl.cmd').touch()
        alias = shim.parent / 'redirect'
        self.junction(target, alias)
        self.env['PHOENIX_PLUGIN_CLI_WORKCTL_PATH'] = str(alias / 'workctl.cmd')
        self.assert_scope_blocked(self.run_probe(), 'runtime_path_unsafe')

    def test_deep_host_exe_target_junction_is_rejected_before_claim(self):
        self.global_host()
        nested_modules = self.cli.parents[2]
        target = self.root / 'accounts/other/modules'
        foreign_exe = target / '@accio-ai/cli-win32-x64/workctl.exe'
        foreign_exe.parent.mkdir(parents=True)
        foreign_exe.touch()
        shutil.rmtree(pilot.filesystem_path(nested_modules))
        self.junction(target, pilot.filesystem_path(nested_modules))
        self.assert_scope_blocked(self.run_probe(), 'runtime_path_unsafe')

    def test_recovery_folder_junction_is_rejected_without_reading_external_records(self):
        self.global_host()
        self.folder.rmdir()
        target = self.root / 'external-records'
        target.mkdir()
        self.junction(target, self.folder)
        result = self.run_probe()
        self.assertEqual(result['code'], 'recovery_unavailable')
        self.assertEqual(self.calls, [])

    def make_records(self, *, duplicate=False):
        cid, agent, prompt = 'synthetic-conversation', 'synthetic-agent', 'SYNTHETIC QUERY ONLY'
        (self.folder / (cid + '.json')).write_text(json.dumps({'id': cid, 'name': 'reception-draft-synthetic', 'agentId': agent}))
        request = {'role': 'req', 'conversationId': cid, 'agentId': agent, 'content': prompt,
                   'requestId': 'synthetic-request', 'messageId': 'synthetic-message'}
        response = {'role': 'res', 'status': 'finished', 'conversationId': cid, 'agentId': agent,
                    'branchParentId': request['messageId'], 'clientRequestId': request['requestId'], 'content': json.dumps(answer())}
        rows = [request, response] + ([response] if duplicate else [])
        (self.folder / (cid + '.message_1.jsonl')).write_text('\n'.join(json.dumps(row) for row in rows))
        (self.folder / 'unrelated.json').write_text(json.dumps({'id': 'unrelated', 'name': 'not-this-task', 'agentId': agent}))
        (self.folder / 'unrelated.message_1.jsonl').write_text('NOT JSON - MUST NOT BE READ')
        return _OwnRecords(str(pilot.filesystem_path(self.cli)), 'reception-draft-synthetic', agent, prompt, time.time(), time.monotonic() + 10)

    def test_real_reader_reads_only_the_uniquely_matched_synthetic_record(self):
        reader = self.make_records()
        result = reader.find()
        self.assertEqual(json.loads(result['answer']), answer())

    def test_real_reader_with_host_long_path_reads_only_matched_synthetic_record(self):
        self.global_host()
        result = self.make_records().find()
        self.assertIsNotNone(result)
        self.assertEqual(json.loads(result['answer']), answer())

    def test_duplicate_matching_responses_are_not_recovered(self):
        self.assertIsNone(self.make_records(duplicate=True).find())


if __name__ == '__main__':
    unittest.main()
