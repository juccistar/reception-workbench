import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, readdir, copyFile, writeFile, lstat } from 'node:fs/promises';
import path from 'node:path';
import { build, Platform } from 'electron-builder';
import { extractFile, listPackage } from '@electron/asar';
import { verifyWhatsAppRuntime } from './whatsapp-runtime.mjs';
import { root, pythonPath } from './paths.mjs';
import { installWindowsIntegrityRetry } from './windows-integrity-retry.mjs';
import { parsePackageArgs, updateConfig, createReleaseFeed } from './release-feed.mjs';
import { loadOrCreateReleaseKeys, RELEASE_PUBLIC_KEY_SHA256, UPDATE_APP_ID } from './update-signing.mjs';

// Builder invokes npm in child shells; retain the Node distribution used for this build.
const nodeBin = path.dirname(process.execPath);
process.env.PATH = nodeBin + path.delimiter + (process.env.PATH || process.env.Path || '');
const manifest=[];
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
function run(command,args,cwd=root) { const r=spawnSync(command,args,{cwd,stdio:'inherit',env:{...process.env,CSC_IDENTITY_AUTO_DISCOVERY:'false'}}); if(r.status!==0)throw new Error('Build command failed; inspect local build output.'); }
async function copyPublicPython(from,to) {
 await mkdir(to,{recursive:true});
 for(const item of await readdir(from,{withFileTypes:true})) {
  if(item.isSymbolicLink()) throw new Error('Source links are not allowed in a distribution.');
  if(item.isDirectory()&&item.name!=='__pycache__') await copyPublicPython(path.join(from,item.name),path.join(to,item.name));
  const source=path.join(from,item.name);
  const relative=path.relative(root,source).replaceAll('\\','/');
  if(item.isFile()&&(item.name.endsWith('.py')||item.name==='SOURCE.txt'||relative==='services/reception/src/reception_service/resources/builtin_policies.json')) {await copyFile(source,path.join(to,item.name));const bytes=await readFile(source);manifest.push({path:relative,bytes:bytes.length,sha256:hash(bytes)});}
 }
}
async function copyTree(from,to,include=()=>true) {
 await mkdir(to,{recursive:true});
 for(const item of await readdir(from,{withFileTypes:true})) {
  if(item.isSymbolicLink())throw new Error('Input links are not allowed.');
  if(!include(item.name))continue;
  const source=path.join(from,item.name),destination=path.join(to,item.name);
  if(item.isDirectory())await copyTree(source,destination,include);else await copyFile(source,destination);
 }
}
async function filesAt(folder,relative='') {
 const result=[];
 for(const entry of await readdir(path.join(folder,relative),{withFileTypes:true})) {
  const name=path.join(relative,entry.name);const file=path.join(folder,name);const stat=await lstat(file);
  if(stat.isSymbolicLink())throw new Error('Distribution cannot contain links.');
  if(entry.isDirectory())result.push(...await filesAt(folder,name));
  else if(name!=='manifest.json'){const bytes=await readFile(file);result.push({path:name.replaceAll('\\','/'),bytes:bytes.length,sha256:hash(bytes)});}
 }
 return result;
}
const { output, candidate, installer, updateUrl } = parsePackageArgs(process.argv.slice(2), root);
// Check the published trust anchor before creating output or compiling anything.
const releaseKeys = installer ? await loadOrCreateReleaseKeys(undefined, { allowCreate: false, expectedPublicKeyFingerprint: RELEASE_PUBLIC_KEY_SHA256 }) : null;
if(existsSync(output) && (await readdir(output)).length) throw new Error('Release output is not empty; choose a new --output directory to preserve it.');
await mkdir(output,{recursive:true});
run(process.execPath,[path.join(root,'scripts/build.mjs')]);
const staging=path.join(root,'artifacts/build',`package-input-${Date.now()}`);
const sourceRoot=path.join(root,'services/reception');
await copyPublicPython(path.join(sourceRoot,'src'),path.join(staging,'python/src'));
await copyFile(path.join(sourceRoot,'run.py'),path.join(staging,'python/run.py'));
const runBytes=await readFile(path.join(sourceRoot,'run.py'));
manifest.push({path:'services/reception/run.py',bytes:runBytes.length,sha256:hash(runBytes)});
await writeFile(path.join(staging,'source-manifest.json'),JSON.stringify({format:1,files:manifest},null,2));
const pythonDist=path.join(staging,'python-dist');
run(pythonPath(),['-m','PyInstaller','--clean','--noconfirm','--noupx','--onedir','--console','--name','reception-backend','--paths',path.join(staging,'python/src'),'--collect-submodules','reception_service','--add-data',path.join(staging,'python/src/reception_service/resources/builtin_policies.json')+path.delimiter+'reception_service/resources','--distpath',pythonDist,'--workpath',path.join(staging,'python-work'),'--specpath',path.join(staging,'python-spec'),path.join(staging,'python/run.py')],path.join(staging,'python'));
const appStage=path.join(staging,'app');
for(const entry of ['main','preload','renderer'])await copyTree(path.join(root,'artifacts/app',entry),path.join(appStage,entry));
const whatsappRuntime = await verifyWhatsAppRuntime(path.join(appStage, 'main'));
function verifyBundledWhatsApp(directory) {
 const archive = path.join(directory, 'resources', 'app.asar');
 const expected = new Map(whatsappRuntime.files.map(file => ['main/' + file.path, file]));
 const listed = listPackage(archive).map(file => file.replaceAll('\\', '/').replace(/^\//, ''));
 for (const entry of listed.filter(file => file.startsWith('main/node_modules/'))) {
  if (!expected.has(entry) && ![...expected.keys()].some(file => file.startsWith(entry + '/'))) throw new Error('Unexpected WhatsApp dependency in packaged application.');
 }
 for (const [entry, file] of expected) {
  const bytes = extractFile(archive, entry);
  if (bytes.length !== file.size || hash(bytes) !== file.sha256) throw new Error('Missing or changed WhatsApp runtime in packaged application.');
 }
 const recorded = JSON.parse(extractFile(archive, 'main/whatsapp-runtime-manifest.json').toString('utf8'));
 if (JSON.stringify(recorded) !== JSON.stringify(whatsappRuntime)) throw new Error('Packaged WhatsApp manifest differs from build input.');
}
const electronDist=path.join(staging,'electron-dist');
await copyTree(path.join(root,'node_modules/electron/dist'),electronDist,name=>!/^(?:logs?|crashpad|crash reports|.*cache.*|accounts|\.runtime|\.venv|tests|node_modules)$/i.test(name)&&!/\.(sqlite3?|db|log)(-(wal|shm))?$/i.test(name));
const pkg=JSON.parse(await readFile(path.join(root,'package.json'),'utf8'));
await writeFile(path.join(appStage,'package.json'),JSON.stringify({name:pkg.name,version:pkg.version,private:true,description:pkg.description,author:'Local application',license:'UNLICENSED',main:'main/index.js'},null,2));
const electronPkg=JSON.parse(await readFile(path.join(root,'node_modules/electron/package.json'),'utf8'));
const guide=path.join(root,'docs/USER_GUIDE.md');
const startupGuide=path.join(root,'docs/自动接待启动与授权.md');
const launchPolicy=path.join(staging,'reception-launch.json');
await writeFile(launchPolicy,JSON.stringify({format:1,app_id:UPDATE_APP_ID,routing:'verified-seller-v1',version:pkg.version},null,2)+'\n');
const launchModules=['connect-live.mjs','merchant-profiles.mjs','merchant-mutex.mjs','launch-identity.mjs','windows-job.mjs','data-location.mjs','release-paths.mjs'];
const restoreIntegrity = installWindowsIntegrityRetry(output);
const baseConfig={appId:'local.inquiry.reception.workbench',productName:'ReceptionWorkbench',directories:{app:appStage,output},electronVersion:electronPkg.version,electronDist,asar:true,files:['main/**/*','preload/**/*','renderer/**/*','package.json'],extraFiles:[{from:path.join(root,'授权启动接待.cmd'),to:'授权启动接待.cmd'},...launchModules.map(name=>({from:path.join(root,'scripts',name),to:'scripts/'+name}))],extraResources:[{from:launchPolicy,to:'reception-launch.json'},{from:path.join(root,'docs/人工接待使用.md'),to:'人工接待使用.md'},{from:startupGuide,to:'自动接待启动与授权.md'},{from:path.join(root,'docs/自动接待连续性.md'),to:'自动接待连续性.md'},{from:path.join(root,'docs/在线升级.md'),to:'在线升级.md'},{from:path.join(pythonDist,'reception-backend'),to:'backend'},{from:path.join(sourceRoot,'src/reception_service/integrations/accio_native/vendor/SOURCE.txt'),to:'backend/SOURCE.txt'},...(existsSync(guide)?[{from:guide,to:'USER_GUIDE.md'}]:[])],npmRebuild:false,forceCodeSigning:false,win:{target:'dir',signAndEditExecutable:false,executableName:'ReceptionWorkbench'}};
try {
await build({projectDir:root,publish:'never',targets:Platform.WINDOWS.createTarget(['dir']),config:structuredClone(baseConfig)});
} finally { restoreIntegrity(); }
const bundle=path.join(output,'win-unpacked');
verifyBundledWhatsApp(bundle);
const indexed=await filesAt(bundle);
const banned=indexed.filter(f=>/(^|\/)(accounts|\.runtime|\.venv|tests|node_modules)(\/|$)/i.test(f.path)||/\.(sqlite3?|db|log)(-(wal|shm))?$/i.test(f.path));
if(banned.length)throw new Error('Unexpected runtime/development data in distribution.');
await writeFile(path.join(bundle,'manifest.json'),JSON.stringify({format:1,kind:'electron-python-reception',version:pkg.version,electron:electronPkg.version,files:indexed},null,2));
const archive=path.join(output,`ReceptionWorkbench-${pkg.version}.zip`);
run(pythonPath(),['-I','-c','import pathlib,sys,zipfile; root=pathlib.Path(sys.argv[1]); target=pathlib.Path(sys.argv[2]); z=zipfile.ZipFile(target,"x",zipfile.ZIP_DEFLATED); [(z.write(p,"ReceptionWorkbench/"+p.relative_to(root).as_posix())) for p in root.rglob("*") if p.is_file()]; z.close()',bundle,archive]);
const archiveHash=hash(await readFile(archive));
await writeFile(archive+'.sha256',archiveHash+'  '+path.basename(archive)+'\n');
const executable=path.join(bundle,'ReceptionWorkbench.exe');
const delivery={executable,archive,archive_sha256:archiveHash,files:indexed.length,bytes:indexed.reduce((sum,f)=>sum+f.bytes,0),signed:false};
if (installer) {
 console.log('已核验现有发布密钥与已发行版本公钥一致。');
 const policyPath=path.join(staging,'update-policy.json');
 await writeFile(policyPath,JSON.stringify({format:1,app_id:UPDATE_APP_ID,public_key:releaseKeys.publicKey},null,2)+'\n');
 const windowsSigning=Boolean(process.env.CSC_LINK || process.env.WIN_CSC_LINK || process.env.CSC_NAME || process.env.CSC_SHA1);
 const signtoolOptions={...(process.env.CSC_NAME?{certificateSubjectName:process.env.CSC_NAME}:{}),...(process.env.CSC_SHA1?{certificateSha1:process.env.CSC_SHA1}:{})};
 const installerOutput=path.join(output,'installer-build');
 const restoreInstallerIntegrity=installWindowsIntegrityRetry(installerOutput);
 try {
  await build({projectDir:root,publish:'never',targets:Platform.WINDOWS.createTarget(['nsis']),config:{...structuredClone(baseConfig),publish:null,extraResources:[...baseConfig.extraResources,{from:policyPath,to:'update-policy.json'}],directories:{app:appStage,output:installerOutput},win:{...baseConfig.win,target:'nsis',signAndEditExecutable:windowsSigning,...(Object.keys(signtoolOptions).length?{signtoolOptions}:{})},nsis:{oneClick:true,perMachine:false,runAfterFinish:false,deleteAppDataOnUninstall:false,include:path.join(root,'scripts/installer.nsh'),artifactName:`ReceptionWorkbench-Setup-${pkg.version}.exe`},afterPack:async context=>{
   verifyBundledWhatsApp(context.appOutDir);
   const bundled=await filesAt(context.appOutDir);
   if(bundled.some(f=>/(^|\/)(accounts|\.runtime|\.venv|tests|node_modules)(\/|$)/i.test(f.path)||/\.(sqlite3?|db|log)(-(wal|shm))?$/i.test(f.path))) throw new Error('Unexpected runtime/development data in installer.');
   const manager=await context.packager.signingManager.value;
   const publisher=await manager.computedPublisherName.value;
   await writeFile(path.join(context.appOutDir,'resources','app-update.yml'),updateConfig(updateUrl,publisher,context.packager.appInfo.updaterCacheDirName));
  }}});
 } finally { restoreInstallerIntegrity(); }
 const name=`ReceptionWorkbench-Setup-${pkg.version}.exe`;
 const setup=path.join(output,name);
 await copyFile(path.join(installerOutput,name),setup);
 await copyFile(path.join(installerOutput,name+'.blockmap'),setup+'.blockmap');
 const sig=spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command','(Get-AuthenticodeSignature -LiteralPath $env:RECEPTION_SETUP_PATH).Status.ToString()'],{encoding:'utf8',env:{...process.env,RECEPTION_SETUP_PATH:setup}});
 if(sig.status!==0) throw new Error('Cannot inspect installer signature.');
 const status=sig.stdout.trim();
 if(!['Valid','NotSigned'].includes(status)) throw new Error(`Installer signature status: ${status}`);
 const feed=await createReleaseFeed({output,installer:setup,version:pkg.version,signed:status==='Valid',privateKey:releaseKeys.privateKey});
 delivery.release_signature='ed25519';
 delivery.installer=setup;
 delivery.installer_signed=status==='Valid';
 delivery.release_feed=feed.feed;
}
await writeFile(path.join(output,'delivery.json'),JSON.stringify(delivery,null,2));
if(!candidate){
 await writeFile(path.join(root,'artifacts/latest-release.path'),path.relative(root,executable)+'\n');
 await writeFile(path.join(root,'artifacts/delivery.json'),JSON.stringify(delivery,null,2));
}
console.log(JSON.stringify({...delivery,candidate}));
