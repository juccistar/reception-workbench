import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, utimesSync, symlinkSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { accountRoot, findWorkctl, launchPlan } from '../connect-live.mjs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
function fixture(){
 const root=mkdtempSync(path.join(os.tmpdir(),'reception-authorized-launch-'));
 const make=(relative,content='synthetic public executable')=>{const file=path.join(root,relative);mkdirSync(path.dirname(file),{recursive:true});writeFileSync(file,content);return file;};
 const executable=make('artifacts/release/win-unpacked/ReceptionWorkbench.exe');
 make('artifacts/latest-release.path',path.relative(root,executable));
 const mcp=make('accounts/account-synthetic/runtime/mcp-client.mjs');
 const node=make('public-node/node.exe');
 const workctl=make('accounts/account-synthetic/plugins/data/cli-tools/plugins/alibaba-com-seller-assistant/tools/workctl/generations/0.1.64/build/prefix/node_modules/@accio-ai/cli/node_modules/@accio-ai/cli-win32-x64/workctl.exe');
 return{root,make,workctl,env:{ACCIO_GATEWAY_TOKEN:'SYNTHETIC_NEVER_PRINT',ACCIO_NODE_BIN:node,ACCIO_MCP_SCRIPT:mcp,PATH:''},cleanup:()=>rmSync(root,{recursive:true,force:true})};
}
test('uses only the account supplied by MCP and public executable generations',()=>{const f=fixture();try{assert.equal(accountRoot(f.env.ACCIO_MCP_SCRIPT),path.join(f.root,'accounts/account-synthetic'));assert.equal(findWorkctl(f.env),f.workctl);}finally{f.cleanup();}});
test('launch arguments never contain inherited credentials and never enable reception/AI',()=>{const f=fixture();try{const plan=launchPlan(f.root,f.env,{seller:'123456789'});assert.equal(plan.workctlFound,true);assert(plan.args.includes('--connect-from-accio'));assert(!JSON.stringify(plan).includes('SYNTHETIC_NEVER_PRINT'));assert(!plan.args.includes('--auto-send'));assert(!plan.args.includes('--check-ai'));assert(plan.args.includes('123456789'));}finally{f.cleanup();}});
test('blocks missing authorization without attempting a process',()=>{const f=fixture();try{assert.throws(()=>launchPlan(f.root,{...f.env,ACCIO_GATEWAY_TOKEN:''}),error=>error.message==='authorized_environment_missing'&&error.missing.includes('ACCIO_GATEWAY_TOKEN'));}finally{f.cleanup();}});
test('rejects an explicitly supplied executable from another account',()=>{const f=fixture();try{const other=f.make('accounts/another-account/public/workctl.exe');assert.throws(()=>findWorkctl(f.env,other),/workctl_invalid/);}finally{f.cleanup();}});
test('does not run an entry outside the project artifacts',()=>{const f=fixture();try{f.make('artifacts/latest-release.path','../external/ReceptionWorkbench.exe');assert.throws(()=>launchPlan(f.root,f.env),/release_invalid/);}finally{f.cleanup();}});
test('launches from a portable folder without the source project pointer',()=>{const f=fixture();try{rmSync(path.join(f.root,'artifacts/latest-release.path'));const executable=f.make('ReceptionWorkbench.exe');const plan=launchPlan(f.root,f.env);assert.equal(plan.executable,executable);}finally{f.cleanup();}});
test('the real CLI blocks absent authorization even with Windows drive casing changed',()=>{const env={SystemRoot:process.env.SystemRoot,PATH:''};let script=fileURLToPath(new URL('../connect-live.mjs',import.meta.url));if(process.platform==='win32')script=script[0].toLowerCase()+script.slice(1);const result=spawnSync(process.execPath,[script],{env,encoding:'utf8',timeout:10000});assert.equal(result.status,2);assert.equal(JSON.parse(result.stderr.trim()).code,'authorized_environment_missing');assert.equal(result.stdout,'');});
test('can leave tool selection to the native UI when no public candidate exists',()=>{const f=fixture();try{const shared=f.make('public-runtime/shared-mcp.mjs');const plan=launchPlan(f.root,{...f.env,ACCIO_MCP_SCRIPT:shared});assert.equal(plan.workctlFound,false);assert(!plan.args.includes('--workctl'));}finally{f.cleanup();}});

const generation='plugins/data/cli-tools/plugins/alibaba-com-seller-assistant/tools/workctl/generations/0.1.64/build/prefix';
function suppliedShim(f,name='workctl.cmd'){
 return f.make(`accounts/account-synthetic/${generation}/${name}`,'UNTRUSTED SHIM CONTENT - MUST NEVER BE READ OR EXECUTED');
}
test('ordinary connection resolves the current host shim with a global MCP and shim-only PATH',()=>{
 const f=fixture();try{
  const shim=suppliedShim(f);
  f.env.ACCIO_MCP_SCRIPT=f.make('Program Files (x86)/Accio/resources/accio-mcp-cli/accio-mcp.mjs');
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=shim;f.env.PATH=path.dirname(shim);
  const plan=launchPlan(f.root,f.env);
  assert.equal(plan.args[plan.args.indexOf('--workctl')+1],f.workctl);
  assert.equal(plan.workctlFound,true);
 }finally{f.cleanup();}
});
test('the host public tool wins over newer shared PATH tools and newer generations',()=>{
 const f=fixture();try{
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=suppliedShim(f);
  const shared=f.make('shared/workctl.exe');f.env.PATH=path.dirname(shared);
  const newer=f.make(`accounts/account-synthetic/${generation.replace('0.1.64','9.9.99')}/node_modules/@accio-ai/cli/node_modules/@accio-ai/cli-win32-x64/workctl.exe`);
  utimesSync(f.workctl,new Date(1000000000000),new Date(1000000000000));
  for(const file of [shared,newer])utimesSync(file,new Date(2000000000000),new Date(2000000000000));
  assert.equal(findWorkctl(f.env),f.workctl);
 }finally{f.cleanup();}
});
test('known explicit cmd, ps1 and extensionless shims map by fixed layout',()=>{
 const f=fixture();try{
  for(const name of ['workctl.cmd','workctl.ps1','workctl'])assert.equal(findWorkctl(f.env,suppliedShim(f,name)),f.workctl);
 }finally{f.cleanup();}
});
test('a known PATH shim resolves without parsing or executing its contents',()=>{
 const f=fixture();try{
  const shim=suppliedShim(f);f.env.PATH=path.dirname(shim);
  assert.equal(findWorkctl(f.env),f.workctl);
 }finally{f.cleanup();}
});
test('arbitrary shims and missing fixed exe targets are rejected without fallback',()=>{
 const f=fixture();try{
  const arbitrary=f.make('accounts/account-synthetic/bin/workctl.cmd',`call "${f.workctl}"`);
  assert.throws(()=>findWorkctl(f.env,arbitrary),/workctl_invalid/);
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=arbitrary;
  assert.throws(()=>findWorkctl(f.env),error=>error.scope_reason==='host_workctl_invalid');
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=suppliedShim(f);rmSync(f.workctl);
  assert.throws(()=>findWorkctl(f.env),error=>error.scope_reason==='host_workctl_invalid');
 }finally{f.cleanup();}
});
test('MCP and host account conflicts block even an explicit tool matching MCP',()=>{
 const f=fixture();try{
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=f.make('accounts/another-account/bin/workctl.exe');
  for(const explicit of ['',f.workctl])assert.throws(()=>findWorkctl(f.env,explicit),error=>error.scope_reason==='account_directory_mismatch');
 }finally{f.cleanup();}
});
test('unbound host or explicit executable is rejected and unbound PATH is skipped',()=>{
 const f=fixture();try{
  const shared=f.make('shared/workctl.exe');
  assert.throws(()=>findWorkctl(f.env,shared),error=>error.scope_reason==='workctl_account_root_missing');
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=shared;
  for(const explicit of ['',f.workctl])assert.throws(()=>findWorkctl(f.env,explicit),error=>error.scope_reason==='account_directory_mismatch');
  delete f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH;
  f.env.PATH=path.dirname(shared);rmSync(f.workctl);
  assert.equal(findWorkctl(f.env),'');
  f.env.ACCIO_MCP_SCRIPT=f.make('shared/mcp.mjs');
  assert.equal(findWorkctl(f.env),'');
  assert.throws(()=>findWorkctl(f.env,shared),error=>error.scope_reason==='account_binding_missing');
 }finally{f.cleanup();}
});
test('bound account tools take precedence over newer shared PATH tools for ordinary connection',()=>{
 const f=fixture();try{
  const shared=f.make('shared/workctl.exe');f.env.PATH=path.dirname(shared);
  utimesSync(f.workctl,new Date(1000000000000),new Date(1000000000000));
  utimesSync(shared,new Date(2000000000000),new Date(2000000000000));
  assert.equal(findWorkctl(f.env),f.workctl);
 }finally{f.cleanup();}
});
test('a host shim through a directory junction is rejected even within the same account',()=>{
 const f=fixture();try{
  const prefix=path.dirname(suppliedShim(f));const moved=prefix+'-actual';
  mkdirSync(moved); // An alias is unsafe even if its target has no account conflict.
  rmSync(prefix,{recursive:true});symlinkSync(moved,prefix,process.platform==='win32'?'junction':'dir');
  f.make(`accounts/account-synthetic/${generation}/workctl.cmd`);
  f.env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH=path.join(prefix,'workctl.cmd');
  assert.throws(()=>findWorkctl(f.env),error=>error.scope_reason==='runtime_path_unsafe');
 }finally{f.cleanup();}
});
test('an explicitly supplied exe cannot escape through a cross-account directory junction',()=>{
 const f=fixture();try{
  const foreign=f.make('accounts/other/bin/workctl.exe');
  const alias=path.join(f.root,'accounts/account-synthetic/redirect');
  symlinkSync(path.dirname(foreign),alias,process.platform==='win32'?'junction':'dir');
  assert.throws(()=>findWorkctl(f.env,path.join(alias,'workctl.exe')),error=>error.scope_reason==='runtime_path_unsafe');
 }finally{f.cleanup();}
});
test('generation discovery refuses junctions above version directories',()=>{
 const f=fixture();try{
  const generations=path.join(f.root,'accounts/account-synthetic',generation.split('/0.1.64/')[0]);
  // Replace the generations root, where checking only version Dirents is insufficient.
  const foreign=path.join(f.root,'accounts/other/generations');mkdirSync(foreign,{recursive:true});
  rmSync(generations,{recursive:true});symlinkSync(foreign,generations,process.platform==='win32'?'junction':'dir');
  assert.equal(findWorkctl(f.env),'');
 }finally{f.cleanup();}
});
