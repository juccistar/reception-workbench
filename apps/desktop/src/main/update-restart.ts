import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { Socket } from 'node:net';
import path from 'node:path';
import { lstat, readFile, mkdir, writeFile } from 'node:fs/promises';
import { windowsArgument, powershellLiteral } from './launch-command';
import { installerJobProgram } from './update-job';
import { rejectDirectoryLinks } from './storage-host';
import { installationGateName, physicalPathProgram } from './installation-guard';
import { realpath } from 'node:fs/promises';

export const INSTALL_APP_ID = 'local.inquiry.reception.workbench';
export async function installedDistribution(resources: string, version: string): Promise<boolean> {
  try {
    const file = path.join(resources, 'reception-installed.json');
    const stat = await lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1024) return false;
    const value = JSON.parse(await readFile(file, 'utf8'));
    return value?.format === 1 && value.app_id === INSTALL_APP_ID && value.version === version;
  } catch { return false; }
}

export interface UpdateRestartPlan {
  executable: string;
  installerPath: string;
  installerSha512: string;
  recoveryPrompt?: boolean;
  version: string;
  previousVersion: string;
  stateDirectory: string;
  parentPid: number;
  args: string[];
  resultFile: string;
}
const launchComponents = ['scripts/connect-live.mjs', 'scripts/merchant-profiles.mjs', 'scripts/merchant-mutex.mjs',
  'scripts/launch-identity.mjs', 'scripts/windows-job.mjs', 'scripts/data-location.mjs', 'scripts/release-paths.mjs',
  'resources/reception-launch.json'];
type Fingerprints = { executable: string; asar: string; backend: string; launchComponents?: { file: string; hash: string | null }[] };
/** The encoded program contains only non-secret paths and arguments. Environment stays in memory. */
export function updateRestartProgram(plan: UpdateRestartPlan, fingerprints?: Fingerprints, installationGate?: string, physicalExecutable?: string): string {
  if (!path.isAbsolute(plan.executable) || path.basename(plan.executable).toLowerCase() !== 'receptionworkbench.exe'
      || !path.isAbsolute(plan.installerPath) || !path.isAbsolute(plan.stateDirectory)
      || !/^[A-Za-z0-9+/]{86}==$/.test(plan.installerSha512)
      || (plan.recoveryPrompt !== undefined && typeof plan.recoveryPrompt !== 'boolean')
      || !path.isAbsolute(plan.resultFile) || !/^\d+\.\d+\.\d+$/.test(plan.version)
      || !/^\d+\.\d+\.\d+$/.test(plan.previousVersion)
      || !Number.isSafeInteger(plan.parentPid) || plan.parentPid <= 0
      || plan.args.some(value => typeof value !== 'string' || value.length > 32768 || /[\u0000\r\n]/.test(value))) {
    throw new Error('升级重启参数无效。');
  }
  const mutex = 'Global\\ReceptionStorage_' + createHash('sha256').update(path.resolve(plan.stateDirectory).toLowerCase()).digest('hex');
  const payload = Buffer.from(JSON.stringify({ executable: plan.executable, installer: plan.installerPath, installer_sha512: plan.installerSha512, recovery_prompt: plan.recoveryPrompt !== false, version: plan.version,
    previous_version: plan.previousVersion, parent_pid: plan.parentPid, argument_line: plan.args.map(windowsArgument).join(' '),
    marker: path.join(path.dirname(plan.executable), 'resources', 'reception-installed.json'),
    asar: path.join(path.dirname(plan.executable), 'resources', 'app.asar'),
    backend: path.join(path.dirname(plan.executable), 'resources', 'backend', 'reception-backend.exe'),
    fingerprints, mutex, installation_gate: installationGate || ('Global\\ReceptionInstallation_' + createHash('sha256').update(path.resolve(plan.executable).toLowerCase()).digest('hex')),
    physical_executable: physicalExecutable || path.resolve(plan.executable), result_file: plan.resultFile, app_id: INSTALL_APP_ID }), 'utf8').toString('base64');
  return `$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${payload}')) | ConvertFrom-Json
function Save-Result($status,$code,$newPid){
  [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($p.result_file))
  $actualVersion=if($code -eq 'previous_version_restored'){$p.previous_version}else{$p.version}
  $value=@{status=$status;code=$code;version=$actualVersion;target_version=$p.version;pid=$newPid;automatic_start_requested=$false} | ConvertTo-Json -Compress
  [IO.File]::WriteAllText($p.result_file,$value,[Text.UTF8Encoding]::new($false))
}
function Marker-Is($version){
  try { $m=[IO.File]::ReadAllText($p.marker,[Text.Encoding]::UTF8) | ConvertFrom-Json; return ($m.format -eq 1 -and $m.app_id -eq $p.app_id -and $m.version -eq $version -and [IO.File]::Exists($p.executable)) } catch { return $false }
}
function Old-Is-Intact {
  try {
    if(-not ((Marker-Is $p.previous_version) -and $p.fingerprints -and
      (Get-FileHash -LiteralPath $p.executable -Algorithm SHA256).Hash -eq $p.fingerprints.executable -and
      (Get-FileHash -LiteralPath $p.asar -Algorithm SHA256).Hash -eq $p.fingerprints.asar -and
      (Get-FileHash -LiteralPath $p.backend -Algorithm SHA256).Hash -eq $p.fingerprints.backend)){return $false}
    $components=@($p.fingerprints.launchComponents)
    if($components.Count -gt 0){
      $present=@($components | Where-Object { $_.hash }).Count
      if(($present -ne 0 -or [version]$p.previous_version -ge [version]'0.2.20') -and $present -ne $components.Count){return $false}
      foreach($component in $components){
        if($component.hash){if((Get-FileHash -LiteralPath $component.file -Algorithm SHA256).Hash -ne $component.hash){return $false}}
        elseif([IO.File]::Exists($component.file)){return $false}
      }
    }
    return $true
  } catch { return $false }
}
function Warn-Recovery {
  if(-not $p.recovery_prompt){return}
  try { Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show('升级未能安全完成。请不要手动启动或覆盖工作区；请联系管理员检查安装程序和备份后恢复。','客户接待工作台升级', 'OK', 'Error') } catch {}
}
${physicalPathProgram}
function Assert-NoOtherProcesses($allowParentTree) {
  $all=@(Get-CimInstance -Query 'SELECT ProcessId, ParentProcessId FROM Win32_Process' -ErrorAction Stop)
  $candidates=@(Get-CimInstance -Query "SELECT ProcessId, ParentProcessId, Name, ExecutablePath FROM Win32_Process WHERE Name = 'ReceptionWorkbench.exe' OR Name = 'reception-backend.exe'" -ErrorAction Stop)
  $byId=@{}; foreach($proc in $all){$byId[[int]$proc.ProcessId]=$proc}
  $exe=[ReceptionPhysicalPath]::Resolve($p.executable)
  $backend=[ReceptionPhysicalPath]::Resolve($p.backend)
  foreach($proc in $candidates){
    if(-not $proc.ExecutablePath){throw 'process_path_unavailable'}
    $candidate=[ReceptionPhysicalPath]::Resolve($proc.ExecutablePath)
    if(-not [String]::Equals($candidate,$exe,[StringComparison]::OrdinalIgnoreCase) -and -not [String]::Equals($candidate,$backend,[StringComparison]::OrdinalIgnoreCase)){continue}
    $allowed=$false; $id=[int]$proc.ProcessId; $seen=@{}
    while($allowParentTree -and $id -gt 0 -and -not $seen.ContainsKey($id)){
      if($id -eq $p.parent_pid){$allowed=$true;break}
      $seen[$id]=$true; if(-not $byId.ContainsKey($id)){break}; $id=[int]$byId[$id].ParentProcessId
    }
    if(-not $allowed){throw 'other_installation_process_running'}
  }
}
$mutex=[Threading.Mutex]::new($false,$p.mutex); $gate=[Threading.Mutex]::new($false,$p.installation_gate); $owned=$false; $gateOwned=$false; $launch=$false; $fallback=$false; $failed=$false; $phase='waiting'; $code='restart_failed'
try {
  try { $gateOwned=$gate.WaitOne(0) } catch [Threading.AbandonedMutexException] { $gateOwned=$true }
  if(-not $gateOwned){[Console]::Out.WriteLine('busy');exit 2}
  try { $owned=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned=$true }
  if(-not $owned){[Console]::Out.WriteLine('busy');exit 2}
  try { Assert-NoOtherProcesses $true } catch { [Console]::Out.WriteLine('busy'); exit 2 }
  [Console]::Out.WriteLine('acquired'); [Console]::Out.Flush()
  $read=[Console]::In.ReadLineAsync()
  if(-not $read.Wait(300000)){exit 0}
  $command=$read.Result
  if($command -ne 'install'){exit 0}
  $parent=Get-Process -Id $p.parent_pid -ErrorAction SilentlyContinue
  if($parent -and -not $parent.WaitForExit(300000)){throw 'old_process_still_running'}
  $phase='parent_exited'
  $stream=[IO.File]::OpenRead($p.installer); $hash=[Security.Cryptography.SHA512]::Create()
  try {
    $actual=[Convert]::ToBase64String($hash.ComputeHash($stream))
    if($actual -ne $p.installer_sha512){throw 'installer_changed'}
    $phase='checking_processes'
    Assert-NoOtherProcesses $false
    ${installerJobProgram}
    $phase='installing'
    $exitCode=[ReceptionInstallerJob]::Run($p.installer,'--updated /S',600000)
  } finally {$stream.Dispose();$hash.Dispose()}
  if($exitCode -eq 0 -and (Marker-Is $p.version)){$launch=$true}
  else {$code='installation_not_completed';$failed=$true}
} catch { $failed=$true; $code='installation_failed' }
finally {
  if($failed -and ($phase -eq 'parent_exited' -or $phase -eq 'installing')){
    try { Assert-NoOtherProcesses $false; if(Old-Is-Intact){$fallback=$true} } catch { $fallback=$false }
  }
  if($owned){$mutex.ReleaseMutex()};$mutex.Dispose(); if($gateOwned){$gate.ReleaseMutex()};$gate.Dispose()
}
if($launch -or $fallback){
  try {
    $started=Start-Process -FilePath $p.executable -ArgumentList $p.argument_line -WorkingDirectory ([IO.Path]::GetDirectoryName($p.executable)) -PassThru
    if($fallback){Save-Result 'started' 'previous_version_restored' $started.Id} else {Save-Result 'started' 'verify_identity_and_reading' $started.Id}
    exit 0
  } catch { $failed=$true; $code='restart_failed' }
}
if($failed){try {Save-Result 'failed' $code $null} catch {}; Warn-Recovery; exit 2}
`;
}

// Electron's patched fs treats app.asar as a virtual directory. Upgrade hashes
// must cover the physical archive bytes; ordinary Node already reads those bytes.
const readPhysicalFile: typeof readFile = process.versions.electron
  ? (require('original-fs') as typeof import('node:fs')).promises.readFile
  : readFile;
async function fingerprint(file: string): Promise<string> {
  return createHash('sha256').update(await readPhysicalFile(file)).digest('hex').toUpperCase();
}
export async function startUpdateRestart(plan: UpdateRestartPlan): Promise<{ commit: () => Promise<void>; cancel: () => Promise<void> }> {
  // Validate before accessing files and calculate fingerprints from the installed program only.
  updateRestartProgram(plan);
  const resources = path.join(path.dirname(plan.executable), 'resources');
  const components = await Promise.all(launchComponents.map(async relative => {
    const file = path.join(path.dirname(plan.executable), relative);
    try {
      const stat = await lstat(file);
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('启动组件不是常规文件。');
      return { file, hash: await fingerprint(file) };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { file, hash: null };
      throw error;
    }
  }));
  const fingerprints = { executable: await fingerprint(plan.executable), asar: await fingerprint(path.join(resources, 'app.asar')),
    backend: await fingerprint(path.join(resources, 'backend', 'reception-backend.exe')), launchComponents: components };
  if (createHash('sha512').update(await readPhysicalFile(plan.installerPath)).digest('base64') !== plan.installerSha512) throw new Error('安装包已变化，请重新下载。');
  const physicalExecutable = await realpath(plan.executable);
  const program = updateRestartProgram(plan, fingerprints, await installationGateName(physicalExecutable), physicalExecutable);
  const scriptsDirectory = path.join(plan.stateDirectory, 'updates', 'helpers');
  await rejectDirectoryLinks(scriptsDirectory); await mkdir(scriptsDirectory, { recursive: true });
  const scriptFile = path.join(scriptsDirectory, `upgrade-${randomUUID()}.ps1`);
  await writeFile(scriptFile, program, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
  const invocation = '& ([ScriptBlock]::Create([IO.File]::ReadAllText(' + powershellLiteral(scriptFile) + ',[Text.Encoding]::UTF8)))';
  const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(executable, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(invocation, 'utf16le').toString('base64')],
    { env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  child.stdin.on('error', () => {}); child.stderr.resume();
  let exited = false;
  const exit = new Promise<void>(resolve => child.once('close', () => { exited = true; resolve(); }));
  await new Promise<void>((resolve, reject) => {
    let settled = false; let output = '';
    const fail = () => { if (settled) return; settled = true; clearTimeout(timer); child.stdin.end(); child.kill(); reject(new Error('升级助手未能取得数据管理锁，尚未安装。')); };
    const timer = setTimeout(fail, 30000);
    child.once('error', fail); child.once('close', fail);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      if (settled) return;
      output += chunk;
      if (output.length > 1024) { fail(); return; }
      if (!output.includes('\n')) return;
      if (output.trim() !== 'acquired') { fail(); return; }
      settled = true; clearTimeout(timer); resolve();
    });
  });
  let action: 'commit' | 'cancel' | undefined;
  let committed: Promise<void> | undefined;
  return {
    commit: () => {
      if (committed) return committed;
      if (action || exited) return Promise.reject(new Error('升级助手已结束，尚未安装。'));
      action = 'commit';
      committed = new Promise<void>((resolve, reject) => {
        child.stdin.once('error', () => reject(new Error('升级助手未收到安装指令。')));
        child.stdin.end('install\n', () => resolve());
      }).then(() => { child.unref(); if (child.stdout instanceof Socket) child.stdout.unref(); if (child.stderr instanceof Socket) child.stderr.unref(); });
      return committed;
    },
    cancel: async () => {
      if (action === 'commit') throw new Error('升级安装已经开始。');
      if (!action) { action = 'cancel'; if (!exited) child.stdin.end('cancel\n'); }
      await exit;
    },
  };
}
