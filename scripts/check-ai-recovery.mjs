import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { accountRoot, resolveWorkctl, samePath } from './connect-live.mjs';

const root=path.resolve(fileURLToPath(new URL('../',import.meta.url)));
const required=['ACCIO_GATEWAY_TOKEN','ACCIO_NODE_BIN','ACCIO_MCP_SCRIPT'];
const fileExists=file=>{try{return !!file&&statSync(file).isFile();}catch{return false;}};
function inspectRuntime(env,workctl) {
 const mcpAccount=accountRoot(env.ACCIO_MCP_SCRIPT);
 const host=env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH?.trim()||'';
 const hostAccount=accountRoot(host);
 const report={status:'inspected',ai_attempts:0,seller_identity_checked:false,
  required_env_present:Object.fromEntries(required.map(key=>[key,!!env[key]?.trim()])),
  node_file_present:fileExists(env.ACCIO_NODE_BIN),mcp_file_present:fileExists(env.ACCIO_MCP_SCRIPT),
  host_workctl_supplied:!!host,host_workctl_account_root_found:!!hostAccount,
  workctl_found:false,mcp_account_root_found:!!mcpAccount,workctl_account_root_found:false,
  account_binding_source:'none',account_binding_found:false,workctl_source:'none',
  account_roots_match:null,runtime_ready:false,scope_reason:'authorized_environment_missing'};
 const result={report,selection:null};
 if(required.some(key=>!report.required_env_present[key]))return result;
 if(!report.node_file_present||!report.mcp_file_present){report.scope_reason='runtime_component_missing';return result;}
 try{result.selection=resolveWorkctl(env,workctl);}catch(error){
  report.scope_reason=error.scope_reason||'workctl_invalid';
  report.workctl_found=fileExists(workctl);
  report.workctl_account_root_found=!!accountRoot(workctl);
  return result;
 }
 const {cli,supplied,cliAccount,bindingSource,source}=result.selection;
 report.workctl_found=!!cli;
 report.workctl_account_root_found=!!cliAccount;
 report.account_binding_source=bindingSource;
 report.account_binding_found=!!supplied;
 report.workctl_source=source;
 report.account_roots_match=supplied&&cliAccount?samePath(supplied,cliAccount):null;
 report.scope_reason=!supplied?'account_binding_missing':!cli?'workctl_not_found':!cliAccount?'workctl_account_root_missing':!report.account_roots_match?'account_directory_mismatch':'runtime_paths_consistent';
 report.runtime_ready=report.scope_reason==='runtime_paths_consistent';
 return result;
}
export function inspectRecoveryRuntime(env,{workctl=''}={}) {return inspectRuntime(env,workctl).report;}
export function recoveryPlan(project,env,{seller,workctl=''}={}) {
 if(!/^[1-9][0-9]*$/.test(seller||''))throw new Error('seller_invalid');
 const {report:inspection,selection}=inspectRuntime(env,workctl);
 if(!inspection.runtime_ready){
  const direct=new Set(['authorized_environment_missing','runtime_component_missing','workctl_invalid']);
  throw Object.assign(new Error(direct.has(inspection.scope_reason)?inspection.scope_reason:'runtime_scope_invalid'),{runtime_diagnostics:inspection});
 }
 const {cli}=selection;
 const python=path.join(project,'services/reception/.venv/Scripts/python.exe');
 if(!fileExists(python))throw new Error('python_missing');
 const output=path.join(project,'artifacts/verification/ai-recovery-pilot');
 if(existsSync(path.join(output,'attempt.claim.json')))throw new Error('attempt_already_claimed');
 return {python,args:['-I','-B',path.join(project,'scripts/check-ai-recovery.py'),'--workctl',cli,'--expect-seller',seller],
         result:path.join(output,'result.json')};
}
async function main() {
 const original=process.argv.slice(2);
 try{
  if(original.filter(value=>value==='--inspect-runtime').length>1)throw new Error('arguments_invalid');
  const inspect=original.includes('--inspect-runtime');
  const argv=original.filter(value=>value!=='--inspect-runtime');
  if(argv.length%2||argv.some((value,index)=>index%2===0&&!['--expect-seller','--workctl'].includes(value)))throw new Error('arguments_invalid');
  if(new Set(argv.filter((_,index)=>index%2===0)).size!==argv.length/2)throw new Error('arguments_invalid');
  const value=name=>{const index=argv.indexOf(name);return index>=0?argv[index+1]:'';};
  const seller=value('--expect-seller');
  if(!/^[1-9][0-9]*$/.test(seller||''))throw new Error('seller_invalid');
  if(inspect){
   const report={...inspectRecoveryRuntime(process.env,{workctl:value('--workctl')}),expected_seller_suffix:seller.slice(-6)};
   const output=path.join(root,'artifacts/verification/ai-recovery-pilot');
   mkdirSync(output,{recursive:true});
   writeFileSync(path.join(output,'runtime-inspection.json'),JSON.stringify(report,null,2));
   console.log(JSON.stringify(report));
   return;
  }
  const plan=recoveryPlan(root,process.env,{seller,workctl:value('--workctl')});
  const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
  const child=spawn(plan.python,plan.args,{cwd:root,env,detached:true,stdio:'ignore',windowsHide:true});
  await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject);});
  child.unref();
  console.log(JSON.stringify({status:'started_result_pending',pid:child.pid,result:plan.result,max_ai_attempts:1,customer_messages_sent:0,feishu_writes:0}));
 }catch(error){
  const codes=new Set(['authorized_environment_missing','seller_invalid','runtime_component_missing','runtime_scope_invalid','workctl_invalid','python_missing','attempt_already_claimed','arguments_invalid']);
  console.error(JSON.stringify({status:'blocked',code:codes.has(error.message)?error.message:'launch_failed',ai_attempts:0,
   ...(error.runtime_diagnostics?{runtime_diagnostics:error.runtime_diagnostics}:{})}));
  process.exitCode=2;
 }
}
const entry=process.argv[1]?path.resolve(process.argv[1]):'';
const self=fileURLToPath(import.meta.url);
if(entry&&(process.platform==='win32'?entry.toLowerCase()===self.toLowerCase():entry===self))await main();
