import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, utimesSync, existsSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { recoveryPlan, inspectRecoveryRuntime } from '../check-ai-recovery.mjs';

function fixture(t){
 const root=mkdtempSync(path.join(tmpdir(),'synthetic-recovery-pilot-'));
 t.after(()=>rmSync(root,{recursive:true,force:true}));
 const put=(file)=>{mkdirSync(path.dirname(file),{recursive:true});writeFileSync(file,'');return file;};
 const account=path.join(root,'accounts/synthetic-account');
 const cli=put(path.join(account,'bin/workctl.exe'));
 const env={ACCIO_GATEWAY_TOKEN:'SYNTHETIC-NON-CREDENTIAL',ACCIO_NODE_BIN:put(path.join(root,'node.exe')),ACCIO_MCP_SCRIPT:put(path.join(account,'mcp.js')),PATH:''};
 put(path.join(root,'services/reception/.venv/Scripts/python.exe'));
 return {root,cli,env,put};
}
test('pilot plan contains one synthetic entry and never an environment secret',t=>{
 const f=fixture(t);const plan=recoveryPlan(f.root,f.env,{seller:'10001',workctl:f.cli});
 assert(plan.args.includes('--expect-seller'));assert(plan.args.includes('10001'));
 assert(plan.args.includes('-I'));assert(!JSON.stringify(plan).includes(f.env.ACCIO_GATEWAY_TOKEN));
 assert(!plan.args.some(value=>value.includes('auto-send')));
});
test('missing inherited authorization is blocked',t=>{
 const f=fixture(t);assert.throws(()=>recoveryPlan(f.root,{}, {seller:'10001',workctl:f.cli}),/authorized_environment_missing/);
});
test('cross-account executable is blocked',t=>{
 const f=fixture(t);const other=f.put(path.join(f.root,'accounts/other/bin/workctl.exe'));
 assert.throws(()=>recoveryPlan(f.root,f.env,{seller:'10001',workctl:other}),/workctl_invalid|runtime_scope_invalid/);
});
test('an existing single-attempt claim cannot be relaunched',t=>{
 const f=fixture(t);f.put(path.join(f.root,'artifacts/verification/ai-recovery-pilot/attempt.claim.json'));
 assert.throws(()=>recoveryPlan(f.root,f.env,{seller:'10001',workctl:f.cli}),/attempt_already_claimed/);
});
test('shared MCP paths mean unresolved account directory, never a seller mismatch',t=>{
 const f=fixture(t);f.env.ACCIO_MCP_SCRIPT=f.put(path.join(f.root,'shared/mcp.js'));
 const inspected=inspectRecoveryRuntime(f.env,{workctl:f.cli});
 assert.equal(inspected.scope_reason,'account_binding_missing');
 assert.equal(inspected.seller_identity_checked,false);assert.equal(inspected.ai_attempts,0);
 assert.equal(inspected.mcp_account_root_found,false);assert.equal(inspected.workctl_account_root_found,true);
 assert(!JSON.stringify(inspected).includes(f.env.ACCIO_GATEWAY_TOKEN));
 assert(!JSON.stringify(inspected).includes(f.root));
 assert.throws(()=>recoveryPlan(f.root,f.env,{seller:'10001',workctl:f.cli}),error=>error.message==='runtime_scope_invalid'&&error.runtime_diagnostics.scope_reason==='account_binding_missing');
});
test('a shared executable is distinguished from an account-bound one',t=>{
 const f=fixture(t);const shared=f.put(path.join(f.root,'shared/workctl.exe'));
 const inspected=inspectRecoveryRuntime(f.env,{workctl:shared});
 assert.equal(inspected.scope_reason,'workctl_account_root_missing');
 assert.equal(inspected.seller_identity_checked,false);
});
test('account public generation is preferred when PATH supplies a shared executable',t=>{
 const f=fixture(t);const shared=f.put(path.join(f.root,'shared/workctl.exe'));
 f.env.PATH=path.dirname(shared);
 const generation=f.put(path.join(f.root,'accounts/synthetic-account/plugins/data/cli-tools/plugins/alibaba-com-seller-assistant/tools/workctl/generations/v1/b1/prefix/node_modules/@accio-ai/cli/node_modules/@accio-ai/cli-win32-x64/workctl.exe'));
 utimesSync(generation,new Date(1000000000000),new Date(1000000000000));
 utimesSync(shared,new Date(2000000000000),new Date(2000000000000));
 const plan=recoveryPlan(f.root,f.env,{seller:'10001'});
 assert.equal(plan.args[plan.args.indexOf('--workctl')+1],generation);
 assert.equal(inspectRecoveryRuntime(f.env).runtime_ready,true);
});
test('runtime inspection never claims that the numeric seller has been verified',t=>{
 const f=fixture(t);const inspected=inspectRecoveryRuntime(f.env,{workctl:f.cli});
 assert.equal(inspected.runtime_ready,true);assert.equal(inspected.seller_identity_checked,false);
 assert.equal(inspected.ai_attempts,0);assert.equal(inspected.account_roots_match,true);
});
const prefix='plugins/data/cli-tools/plugins/alibaba-com-seller-assistant/tools/workctl/generations/0.1.64/mvuec994-57222bb83950/prefix';
function globalHost(f){
 f.env.ACCIO_MCP_SCRIPT=f.put(path.join(f.root,'Program Files (x86)/Accio/resources/accio-mcp-cli/accio-mcp.mjs'));
 const shim=f.put(path.join(f.root,'accounts/synthetic-account',prefix,'workctl.cmd'));
 const exe=f.put(path.join(path.dirname(shim),'node_modules/@accio-ai/cli/node_modules/@accio-ai/cli-win32-x64/workctl.exe'));
 f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=shim;f.env.PATH=path.dirname(shim);
 return {shim,exe};
}
test('global MCP plus current host shim binds recovery without implying seller verification or claiming an attempt',t=>{
 const f=fixture(t);const {exe}=globalHost(f);
 const inspected=inspectRecoveryRuntime(f.env);
 assert.equal(inspected.runtime_ready,true);
 assert.equal(inspected.mcp_account_root_found,false);
 assert.equal(inspected.host_workctl_account_root_found,true);
 assert.equal(inspected.account_binding_source,'host_workctl');
 assert.equal(inspected.workctl_source,'host_workctl');
 assert.equal(inspected.account_roots_match,true);
 assert.equal(inspected.seller_identity_checked,false);assert.equal(inspected.ai_attempts,0);
 assert(!JSON.stringify(inspected).includes(f.env.ACCIO_GATEWAY_TOKEN));assert(!JSON.stringify(inspected).includes(f.root));
 const plan=recoveryPlan(f.root,f.env,{seller:'10001'});
 assert.equal(plan.args[plan.args.indexOf('--workctl')+1],exe);
 assert(!existsSync(path.join(f.root,'artifacts/verification/ai-recovery-pilot/attempt.claim.json')));
});
test('both matching supplied account sources are recorded',t=>{
 const f=fixture(t);f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=f.cli;
 const inspected=inspectRecoveryRuntime(f.env,{workctl:f.cli});
 assert.equal(inspected.account_binding_source,'mcp_and_host_workctl');assert.equal(inspected.runtime_ready,true);
});
test('host binding accepts an explicitly selected known shim and still checks native seller later',t=>{
 const f=fixture(t);const {shim,exe}=globalHost(f);
 const plan=recoveryPlan(f.root,f.env,{seller:'10001',workctl:shim});
 assert.equal(plan.args[plan.args.indexOf('--workctl')+1],exe);
 assert.equal(inspectRecoveryRuntime(f.env,{workctl:shim}).workctl_source,'explicit');
});
test('current host tool is preferred to a newer shared PATH tool',t=>{
 const f=fixture(t);const {exe}=globalHost(f);
 const shared=f.put(path.join(f.root,'shared/workctl.exe'));f.env.PATH=path.dirname(shared);
 utimesSync(exe,new Date(1000000000000),new Date(1000000000000));utimesSync(shared,new Date(2000000000000),new Date(2000000000000));
 const plan=recoveryPlan(f.root,f.env,{seller:'10001'});
 assert.equal(plan.args[plan.args.indexOf('--workctl')+1],exe);
});
test('global MCP and PATH account shim alone do not establish a trusted binding',t=>{
 const f=fixture(t);globalHost(f);delete f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH;
 const inspected=inspectRecoveryRuntime(f.env);
 assert.equal(inspected.workctl_found,false);assert.equal(inspected.workctl_account_root_found,false);
 assert.equal(inspected.account_binding_source,'none');assert.equal(inspected.scope_reason,'account_binding_missing');
 assert.equal(inspected.runtime_ready,false);
});
test('MCP and host conflicts are blocked before native identity or any attempt claim',t=>{
 const f=fixture(t);globalHost(f);
 f.env.ACCIO_MCP_SCRIPT=f.put(path.join(f.root,'accounts/other/mcp.mjs'));
 const inspected=inspectRecoveryRuntime(f.env);
 assert.equal(inspected.scope_reason,'account_directory_mismatch');assert.equal(inspected.runtime_ready,false);
 assert.equal(inspected.seller_identity_checked,false);assert.equal(inspected.ai_attempts,0);
 assert.throws(()=>recoveryPlan(f.root,f.env,{seller:'10001'}),/runtime_scope_invalid/);
 assert(!existsSync(path.join(f.root,'artifacts')));
});
test('global MCP host binding rejects an explicit executable from a foreign account',t=>{
 const f=fixture(t);globalHost(f);const other=f.put(path.join(f.root,'accounts/other/workctl.exe'));
 assert.equal(inspectRecoveryRuntime(f.env,{workctl:other}).scope_reason,'account_directory_mismatch');
 assert.throws(()=>recoveryPlan(f.root,f.env,{seller:'10001',workctl:other}),/runtime_scope_invalid/);
});
test('untrusted host shim fails closed despite a usable explicit exe',t=>{
 const f=fixture(t);globalHost(f);
 f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=f.put(path.join(f.root,'accounts/synthetic-account/bin/workctl.cmd'));
 assert.equal(inspectRecoveryRuntime(f.env,{workctl:f.cli}).scope_reason,'host_workctl_invalid');
});
test('an MCP account path redirected by a junction cannot authorize recovery',t=>{
 const f=fixture(t);const target=f.put(path.join(f.root,'accounts/other/runtime/mcp.mjs'));
 const alias=path.join(f.root,'accounts/synthetic-account/redirect');
 symlinkSync(path.dirname(target),alias,process.platform==='win32'?'junction':'dir');
 f.env.ACCIO_MCP_SCRIPT=path.join(alias,'mcp.mjs');
 const inspected=inspectRecoveryRuntime(f.env,{workctl:f.cli});
 assert.equal(inspected.scope_reason,'runtime_path_unsafe');assert.equal(inspected.runtime_ready,false);
});
test('the actual CLI exits blocked without starting AI when no authorization is inherited',()=>{
 const env={SystemRoot:process.env.SystemRoot,PATH:''};
 const script=fileURLToPath(new URL('../check-ai-recovery.mjs',import.meta.url));
 const result=spawnSync(process.execPath,[script,'--expect-seller','10001'],{env,encoding:'utf8'});
 assert.equal(result.status,2);
 const report=JSON.parse(result.stderr.trim());
 assert.equal(report.code,'authorized_environment_missing');assert.equal(report.ai_attempts,0);
});
