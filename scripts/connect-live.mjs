import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readdirSync, readFileSync, realpathSync, statSync, lstatSync, mkdirSync, writeFileSync, renameSync, unlinkSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isReleasePath } from './release-paths.mjs';
import { probeNativeSeller } from './launch-identity.mjs';
import { AUTO_LAUNCH_CONTEXT_ENV, merchantRegistryDirectory, merchantSupportPaths, resolveMerchantProfile, maintenanceEnvironment, workspacePathUnits } from './merchant-profiles.mjs';

export const projectRoot=path.resolve(fileURLToPath(new URL('../',import.meta.url)));
const required=['ACCIO_GATEWAY_TOKEN','ACCIO_NODE_BIN','ACCIO_MCP_SCRIPT'];
export function accountRoot(file) {
 if(!file)return null;
 let current=path.dirname(path.resolve(file));
 while(path.dirname(current)!==current){if(path.basename(path.dirname(current)).toLowerCase()==='accounts')return current;current=path.dirname(current);}
 return null;
}
function fileExists(file){try{return !!file&&statSync(file).isFile();}catch{return false;}}
const generationsRelative='plugins/data/cli-tools/plugins/alibaba-com-seller-assistant/tools/workctl/generations';
const exeRelative='node_modules/@accio-ai/cli/node_modules/@accio-ai/cli-win32-x64/workctl.exe';
const comparable=file=>process.platform==='win32'?file.toLowerCase():file;
export const samePath=(left,right)=>!!left&&!!right&&comparable(left)===comparable(right);
function invalid(scopeReason='workctl_invalid'){return Object.assign(new Error('workctl_invalid'),{scope_reason:scopeReason});}
function realPathUnchanged(file){
 try{return samePath(path.resolve(file),realpathSync.native(file));}catch{return false;}
}
function safeDirectory(folder){try{return statSync(folder).isDirectory()&&realPathUnchanged(folder);}catch{return false;}}
function publicTool(file){
 if(!fileExists(file))throw invalid();
 if(!realPathUnchanged(file))throw invalid('runtime_path_unsafe');
 const absolute=path.resolve(file);
 if(path.basename(absolute).toLowerCase()==='workctl.exe')return absolute;
 // Recognize only Accio's documented generation prefix. Do not read, parse or
 // execute shim contents, including when the caller explicitly supplies a shim.
 const account=accountRoot(absolute);
 const parts=account?path.relative(account,absolute).split(path.sep):[];
 const expected=[...generationsRelative.split('/'),null,null,'prefix',null];
 if(parts.length!==expected.length||!expected.every((part,index)=>part===null||comparable(parts[index])===comparable(part))
    ||!['workctl','workctl.cmd','workctl.ps1'].includes(path.basename(absolute).toLowerCase()))throw invalid();
 const executable=path.join(path.dirname(absolute),exeRelative);
 if(!fileExists(executable))throw invalid();
 if(!realPathUnchanged(executable)||!samePath(accountRoot(executable),account))throw invalid('runtime_path_unsafe');
 return executable;
}
export function resolveWorkctl(env,explicit='') {
 const mcpAccount=accountRoot(env.ACCIO_MCP_SCRIPT);
 if(env.ACCIO_MCP_SCRIPT&&!realPathUnchanged(env.ACCIO_MCP_SCRIPT))throw invalid('runtime_path_unsafe');
 const host=env.PHOENIX_PLUGIN_CLI_WORKCTL_PATH?.trim()||'';
 let hostCli='';
 if(host){
  if(!path.isAbsolute(host))throw invalid('host_workctl_invalid');
  try{hostCli=publicTool(host);}catch(error){throw invalid(error.scope_reason==='runtime_path_unsafe'?'runtime_path_unsafe':'host_workctl_invalid');}
 }
 const hostAccount=hostCli?accountRoot(hostCli):null;
 if(hostCli&&!hostAccount)throw invalid('account_directory_mismatch');
 if(mcpAccount&&hostAccount&&!samePath(mcpAccount,hostAccount))throw invalid('account_directory_mismatch');
 const supplied=mcpAccount||hostAccount;
 const bindingSource=mcpAccount?(hostAccount?'mcp_and_host_workctl':'mcp'):(hostAccount?'host_workctl':'none');
 const selection=(cli,source)=>({cli,supplied,mcpAccount,hostAccount,cliAccount:cli?accountRoot(cli):null,bindingSource,source});
 const belongs=file=>!!supplied&&samePath(accountRoot(file),supplied);
 if(explicit){
  if(!supplied)throw invalid('account_binding_missing');
  if(!accountRoot(explicit))throw invalid('workctl_account_root_missing');
  if(!belongs(explicit))throw invalid('account_directory_mismatch');
  return selection(publicTool(explicit),'explicit');
 }
 if(hostCli)return selection(hostCli,'host_workctl');
 const candidates=[];
 for(const raw of (env.PATH||env.Path||'').split(path.delimiter)){
  const folder=raw.replace(/^"(.*)"$/,'$1');
  if(!path.isAbsolute(folder)||!belongs(path.join(folder,'workctl.exe')))continue;
  for(const name of ['workctl.exe','workctl.cmd','workctl.ps1','workctl']){
   try{candidates.push({cli:publicTool(path.join(folder,name)),source:'path'});}catch{/* Ignore unavailable/unsafe PATH entries. */}
  }
 }
 // Enumerate only public generations below the already bound account. Check
 // each actual directory before listing, so junctions cannot reach other accounts.
 if(supplied){
  const generations=path.join(supplied,generationsRelative);
  if(safeDirectory(generations))for(const version of readdirSync(generations,{withFileTypes:true}).filter(item=>item.isDirectory()&&!item.isSymbolicLink())){
   const versionFolder=path.join(generations,version.name);
   if(!safeDirectory(versionFolder))continue;
   for(const build of readdirSync(versionFolder,{withFileTypes:true}).filter(item=>item.isDirectory()&&!item.isSymbolicLink())){
    const buildFolder=path.join(versionFolder,build.name);
    if(!safeDirectory(buildFolder))continue;
    try{candidates.push({cli:publicTool(path.join(buildFolder,'prefix',exeRelative)),source:'account_generation'});}catch{/* Never follow redirected executable paths. */}
   }
  }
 }
 candidates.sort((a,b)=>Number(samePath(accountRoot(b.cli),supplied))-Number(samePath(accountRoot(a.cli),supplied))||statSync(b.cli).mtimeMs-statSync(a.cli).mtimeMs);
 return selection(candidates[0]?.cli||'',candidates[0]?.source||'none');
}
export function findWorkctl(env,explicit='') {return resolveWorkctl(env,explicit).cli;}
export function launchPlan(root,env,options={}) {
 const missing=required.filter(name=>!env[name]?.trim());
 if(missing.length)throw Object.assign(new Error('authorized_environment_missing'),{missing});
 if(!fileExists(env.ACCIO_NODE_BIN)||!fileExists(env.ACCIO_MCP_SCRIPT))throw new Error('runtime_component_missing');
 const pointer=path.join(root,'artifacts/latest-release.path');
 let executable;
 if(fileExists(pointer)){
  executable=path.resolve(root,readFileSync(pointer,'utf8').trim());
  if(!isReleasePath(root,executable)||path.basename(executable)!=='ReceptionWorkbench.exe'||!fileExists(executable))throw new Error('release_invalid');
 }else{
  executable=path.join(root,'ReceptionWorkbench.exe');
  if(!fileExists(executable))throw new Error('release_missing');
 }
 const workctl=findWorkctl(env,options.workctl||'');
 const args=['--connect-from-accio'];
 if(workctl)args.push('--workctl',workctl);
 if(options.seller){if(!/^[1-9][0-9]*$/.test(options.seller))throw new Error('seller_invalid');args.push('--expect-seller',options.seller);}
 if(options.dataRoot)args.push('--data-dir',path.resolve(options.dataRoot));
 return {executable,args,workctlFound:!!workctl};
}
function launcherFile(file) {
 if(typeof file!=='string'||!path.isAbsolute(file)||/[\x00-\x1f]/.test(file)||!fileExists(file)||!realPathUnchanged(file))throw new Error('release_invalid');
 return path.resolve(file);
}
function currentProgram(root,env,options) {
 if(options.developmentEntry){
  const executable=launcherFile(options.currentExecutable);
  const entry=launcherFile(options.developmentEntry);
  const expectedExecutable=path.join(root,'node_modules/electron/dist',process.platform==='win32'?'electron.exe':'electron');
  if(!samePath(executable,expectedExecutable)||!samePath(entry,path.join(root,'artifacts/app/main/index.js')))throw new Error('release_invalid');
  return {executable,prefix:[entry],development:true};
 }
 let executable;
 if(options.currentExecutable){
  executable=launcherFile(options.currentExecutable);
  const local=path.join(root,'ReceptionWorkbench.exe');
  if(path.basename(executable).toLowerCase()!=='receptionworkbench.exe'||!(samePath(executable,local)||isReleasePath(root,executable)))throw new Error('release_invalid');
 }else executable=launchPlan(root,env,options).executable;
 // Do not pair a new routing kit with an old application that cannot consume it.
 const policyFile=path.join(path.dirname(executable),'resources/reception-launch.json');
 if(!fileExists(policyFile)||!realPathUnchanged(policyFile)||statSync(policyFile).size>4096)throw new Error('launch_components_missing');
 let policy;try{policy=JSON.parse(readFileSync(policyFile,'utf8'));}catch{throw new Error('launch_components_missing');}
 if(policy?.format!==1||policy.app_id!=='local.inquiry.reception.workbench'||policy.routing!=='verified-seller-v1')throw new Error('launch_components_missing');
 return {executable,prefix:[],development:false};
}
function ownershipBackend(root,program,env){
 if(!program.development){
  const executable=launcherFile(path.join(path.dirname(program.executable),'resources/backend/reception-backend.exe'));
  return {executable,prefix:[],cwd:path.dirname(executable),env:{...env,PYTHONIOENCODING:'utf-8'}};
 }
 const candidates=[env.RECEPTION_PYTHON,path.join(root,'services/reception/.venv/Scripts/python.exe'),path.join(root,'services/reception/.venv/bin/python')];
 const executable=candidates.find(file=>file&&fileExists(file));
 if(!executable)throw new Error('runtime_component_missing');
 return {executable:launcherFile(executable),prefix:['-I',launcherFile(path.join(root,'services/reception/run.py'))],cwd:root,env:{...env,PYTHONIOENCODING:'utf-8'}};
}
function writeLaunchDiagnostic(registry, seller, dataRoot, ownerStatus, code) {
 try {
  if(!safeDirectory(registry))return undefined;
  const dir=path.join(registry,'diagnostics');
  try { mkdirSync(dir,{mode:0o700}); } catch(error) { if(error.code!=='EEXIST')throw error; }
  if(!safeDirectory(dir))return undefined;
  const id=randomUUID(), temporary=path.join(dir,`.${id}.tmp`), file=path.join(dir,`${id}.json`);
  try {
   writeFileSync(temporary,JSON.stringify({version:'0.2.21',seller,registry,dataRoot:dataRoot||null,ownerStatus,code}),{flag:'wx',mode:0o600});
   renameSync(temporary,file);
  } finally { try { unlinkSync(temporary); } catch {} }
  return file;
 } catch { return undefined; }
}
export function runRecoveryProcess(backend, args, timeoutMs = 30 * 60 * 1000) {
 return new Promise((resolve, reject) => {
  const child=spawn(backend.executable,[...backend.prefix,...args],{env:maintenanceEnvironment(backend.env),cwd:backend.cwd,windowsHide:true,stdio:['pipe','pipe','pipe']});
  let output='', oversized=false, failed=false, timedOut=false;
  child.stdin.on('error',()=>{});
  child.stdout.setEncoding('utf8');
  child.stdout.on('data',chunk=>{if(output.length+chunk.length>16384)oversized=true;else output+=chunk;});
  child.stderr.on('data',()=>{});
  child.once('error',()=>{failed=true;});
  const timer=setTimeout(()=>{timedOut=true;child.stdin.write('{"command":"cancel"}\n');},timeoutMs);
  child.once('close',code=>{
   clearTimeout(timer);
   if(failed||oversized||code!==0){reject(new Error(timedOut?'merchant_recovery_timeout':'merchant_initialization_incomplete'));return;}
   try { resolve(JSON.parse(output.trim())); } catch { reject(new Error('merchant_initialization_incomplete')); }
  });
 });
}
async function recoverIncomplete(context, backend, recover) {
 const {dataDirectory,backupDirectory}=merchantSupportPaths(context.registry);
 const destination=path.join(dataDirectory,`${context.seller}-recovery-${randomUUID().slice(0,8)}`);
 if(samePath(destination,context.dataRoot))throw new Error('merchant_initialization_incomplete');
 if(workspacePathUnits(destination)>240)throw new Error('merchant_workspace_path_too_long');
 try { lstatSync(destination); throw new Error('merchant_registration_conflict'); } catch(error) { if(error.code!=='ENOENT')throw error; }
 const args=['--maintenance','recover-profile','--source',context.registry,'--seller-id',context.seller,'--destination',destination,'--backup-dir',backupDirectory];
 let result;
 try {
  if(recover) result=await recover(args);
  else result=await runRecoveryProcess(backend,args);
 } catch(error) {
  if(error.message==='merchant_recovery_timeout')throw error;
  throw new Error('merchant_initialization_incomplete');
 }
 if(result?.status!=='completed'||result.action!=='recover-profile'||!samePath(result.data_root,destination))throw new Error('merchant_initialization_incomplete');
}
export async function launchAuthorized(root,env,options={}){
 root=path.resolve(root);
 const missing=required.filter(name=>typeof env[name]!=='string'||!env[name].trim());
 if(missing.length)throw Object.assign(new Error('authorized_environment_missing'),{missing});
 if(!fileExists(env.ACCIO_NODE_BIN)||!fileExists(env.ACCIO_MCP_SCRIPT))throw new Error('runtime_component_missing');
 if(options.seller&&(typeof options.seller!=='string'||!/^[1-9][0-9]{0,127}$/.test(options.seller)))throw new Error('seller_invalid');
 const program=currentProgram(root,env,options);
 const selected=resolveWorkctl(env,options.workctl||'');
 if(!selected.cli)throw new Error('workctl_invalid');
 const sellerId=await (options.probe||probeNativeSeller)({cli:selected.cli,env,tempRoot:options.tempRoot});
 if(typeof sellerId!=='string'||!/^[1-9][0-9]{0,127}$/.test(sellerId))throw new Error('identity_probe_invalid');
 if(options.seller&&options.seller!==sellerId)throw new Error('seller_mismatch');
 const backend=options.backend||ownershipBackend(root,program,env);
 const profileOptions={dataRoot:options.dataRoot?path.resolve(options.dataRoot):undefined,
  programDirectory:path.dirname(program.executable),backend,inspectOwnership:options.inspectOwnership};
 let profile;
 try {
  try { profile=await resolveMerchantProfile(sellerId,env,profileOptions); }
  catch(error) {
   if(error.message!=='merchant_initialization_incomplete'||!error.context||options.dataRoot)throw error;
   // resolveMerchantProfile releases the registry mutex in its finally before returning here.
   try { await recoverIncomplete(error.context,backend,options.recoverProfile); }
   catch(recoveryError) { recoveryError.context=error.context; throw recoveryError; }
   profile=await resolveMerchantProfile(sellerId,env,profileOptions);
  }
 } catch(error) {
  const allowed=new Set(['merchant_recovery_timeout','merchant_initialization_incomplete','merchant_workspace_path_too_long','merchant_workspace_missing','merchant_workspace_mismatch','merchant_workspace_ownership_unknown','merchant_ownership_unavailable','merchant_registration_conflict','merchant_path_conflict','merchant_profile_root_conflict','merchant_registry_invalid','merchant_path_invalid','registry_lock_unavailable']);
  const code=allowed.has(error.message)?error.message:'launch_failed';
  const context=error.context;
  try {
   const diagnostic=writeLaunchDiagnostic(merchantRegistryDirectory(env),sellerId,context?.dataRoot,context?.reason==='zero_byte_workspace'?'unknown':'unavailable',code);
   if(diagnostic)error.diagnostic=diagnostic;
  } catch { /* Diagnostics must never replace the original failure. */ }
  throw error;
 }
 const childEnv={...env,RECEPTION_BOOTSTRAP_DIR:profile.stateDirectory,RECEPTION_MERCHANT_REGISTRY_DIR:merchantRegistryDirectory(env)};
 delete childEnv.ELECTRON_RUN_AS_NODE;
 childEnv[AUTO_LAUNCH_CONTEXT_ENV]=JSON.stringify(profile);
 return {...profile,executable:program.executable,args:[...program.prefix,'--connect-from-accio','--expect-seller',sellerId,'--data-dir',profile.dataRoot,'--workctl',selected.cli],env:childEnv,workctlFound:true};
}
async function main(){
 const argv=process.argv.slice(2);
 const option=name=>{const index=argv.indexOf(name);return index>=0?argv[index+1]||'':'';};
 try{
  const allowed=new Set(['--workctl','--expect-seller','--data-dir','--current-executable','--development-entry']),seen=new Set();
  if(argv.length%2)throw new Error('arguments_invalid');
  for(let index=0;index<argv.length;index+=2){if(!allowed.has(argv[index])||seen.has(argv[index])||!argv[index+1]||argv[index+1].startsWith('--')||/[\x00-\x1f]/.test(argv[index+1]))throw new Error('arguments_invalid');seen.add(argv[index]);}
  const plan=await launchAuthorized(projectRoot,process.env,{workctl:option('--workctl'),seller:option('--expect-seller'),dataRoot:option('--data-dir'),currentExecutable:option('--current-executable'),developmentEntry:option('--development-entry')});
  const child=spawn(plan.executable,plan.args,{cwd:path.dirname(plan.executable),env:plan.env,detached:true,stdio:'ignore',windowsHide:false});
  await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject);});
  child.unref();
  console.log(JSON.stringify({status:'launched_read_check_pending'}));
 }catch(error){
  const codes={authorized_environment_missing:'Open this command in the current authorized Accio terminal.',runtime_component_missing:'The inherited Accio runtime component is unavailable.',release_missing:'Build or extract the complete desktop release first.',release_invalid:'The release entry is invalid.',workctl_invalid:'Choose a workctl.exe belonging to the current Accio account.',seller_invalid:'Expected seller ID must use positive ASCII digits.',arguments_invalid:'Use --workctl PATH, --expect-seller ID, or --data-dir PATH.'};
  Object.assign(codes,{
   launch_components_missing:'Install the complete 0.2.20 or newer package before using automatic seller routing.',
   seller_mismatch:'The current authorized seller differs from the requested seller; no workspace was opened.',
   identity_probe_invalid:'The native interface did not return a valid seller identity.',identity_probe_failed:'The current authorized seller could not be verified.',
   identity_probe_timeout:'Seller verification timed out; no business workspace was opened.',identity_probe_output_limit:'Seller verification returned an invalid response.',
   merchant_workspace_missing:'The saved merchant workspace is missing; no empty replacement was created.',
   merchant_profile_unavailable:'The merchant startup configuration is unavailable; preserve it for recovery.',
   merchant_profile_root_conflict:'The requested directory differs from this merchant saved workspace.',
   merchant_workspace_mismatch:'The directory belongs to another merchant; no data was changed.',
   merchant_legacy_ownership_unknown:'The original workspace ownership is not unique; preserve it and verify it before using a new workspace.',
   merchant_workspace_ownership_unknown:'The saved workspace ownership could not be verified; no data was changed.',
   merchant_ownership_unavailable:'Workspace ownership metadata could not be checked; no data was changed.',
   merchant_path_invalid:'Use an existing safe local path without directory links.',merchant_path_conflict:'Merchant data and startup directories must remain isolated.',
   merchant_registry_invalid:'The merchant registry is invalid; preserve it for recovery.',merchant_registry_unavailable:'The merchant registry location is unavailable.',
   merchant_registration_conflict:'An incomplete or conflicting registration was preserved; verify its configuration before retrying.',
   registry_lock_unavailable:'Another startup is updating merchant configuration; retry after it finishes.',
   merchant_workspace_path_too_long:'The workspace path is too long; select a shorter data directory or migrate existing data manually.',
   merchant_initialization_incomplete:'The previous workspace initialization is incomplete; preserve the directory for recovery.',
   merchant_recovery_timeout:'Recovery timed out; verify the recovery process and preserved directories before retrying.'
  });
  const code=Object.hasOwn(codes,error.message)?error.message:'launch_failed';
  console.error(JSON.stringify({status:'blocked',code}));
  process.exitCode=2;
 }
}
const entry=process.argv[1]?path.resolve(process.argv[1]):'';
const modulePath=fileURLToPath(import.meta.url);
if(entry&&(process.platform==='win32'?entry.toLowerCase()===modulePath.toLowerCase():entry===modulePath))await main();
