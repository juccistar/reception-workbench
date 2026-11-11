import path from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { lstat, mkdir } from 'node:fs/promises';
import type { StorageRequest } from '../shared/api';
import { maintenanceEnvironment, merchantSupportPaths } from '../../../../scripts/merchant-profiles.mjs';

export interface MaintenanceResult { status: 'completed'; action: string; data_root: string; backup: string | null; recovery_backup?: string | null; [key: string]: unknown }
const maintenanceMessages: Record<string, string> = {
  workspace_busy: '工作区正在使用，请正常退出该商家的旧窗口后重试。',
  storage_busy: '工作区正在使用，请正常退出该商家的旧窗口后重试。',
  storage_path_too_long: '数据目录路径过长，请选择较短的目录。',
  storage_backup_failed: '启动恢复备份未完成，请保留原目录并检查备份位置。',
  storage_recovery_failed: '启动恢复校验未通过，请保留原目录和恢复快照。',
  storage_recovery_incomplete: '启动恢复尚未完成，请保留原目录和恢复快照以便核查。',
  storage_recovery_required: '工作区需要核查关闭状态，请保留原数据并使用原版本恢复。',
  storage_recovery_unavailable: '当前环境不支持安全启动恢复，请保留原数据。',
  storage_upgrade_requires_clean: '升级前需要正常关闭并备份，请先使用旧版本打开工作区并正常退出后重试。',
};
/** Per-registry namespace also keeps backup paths short for isolated Accio homes. */
export function safetyBackupParent(stateDirectory: string, registryDirectory?: string, protectedDirectories: string[] = []): string {
  const parent = merchantSupportPaths(registryDirectory || stateDirectory).backupDirectory;
  for (const reserved of [stateDirectory, registryDirectory, ...protectedDirectories].filter((value): value is string => !!value)) {
    if (related(parent, reserved) || related(reserved, parent)) throw new StorageError('reserved_directory', '安全备份目录与工作区或程序位置冲突，请保留原数据并核查目录。');
  }
  return parent;
}
export interface MaintenanceCommand { executable: string; prefix: string[]; cwd: string; env: NodeJS.ProcessEnv }
export class StorageError extends Error {
  constructor(public code: string, message: string) { super(message); this.name = 'StorageError'; }
}
export function cleanStorageRequest(input: unknown): StorageRequest {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new StorageError('invalid_request', '请选择数据管理操作。');
  const value = input as Record<string, unknown>;
  if (Object.keys(value).some(key => !['action', 'target', 'backup'].includes(key)) || typeof value.action !== 'string' || !['migrate', 'open', 'backup', 'restore'].includes(value.action)) throw new StorageError('invalid_request', '数据管理参数无效。');
  const action = value.action as StorageRequest['action'];
  const target = absoluteDirectory(value.target);
  if (action === 'restore') return { action, target, backup: absoluteDirectory(value.backup) };
  if (value.backup !== undefined) throw new StorageError('invalid_request', '此操作不接受备份来源。');
  return { action, target };
}
export function absoluteDirectory(value: unknown): string {
  if (typeof value !== 'string' || !value.trim() || value.length > 32768 || /[\u0000-\u001f]/.test(value) || !path.isAbsolute(value.trim()) || /^[\\/]{2}/.test(value.trim())) throw new StorageError('invalid_directory', '请输入本机文件夹的完整绝对路径。');
  const result = path.normalize(value.trim());
  if (result === path.parse(result).root) throw new StorageError('invalid_directory', '请选择磁盘中的专用文件夹，不能直接使用磁盘根目录。');
  return result;
}
function related(a: string, b: string) {
  const relative = path.relative(a, b);
  return !relative || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative));
}
export async function rejectDirectoryLinks(directory: string) {
  for (let current = directory;;) {
    try { if ((await lstat(current)).isSymbolicLink()) throw new StorageError('unsafe_directory', '数据目录不能经过符号链接或目录联接。'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    const parent = path.dirname(current); if (parent === current) break; current = parent;
  }
}
export async function validateStorageDestination(request: StorageRequest, currentRoot: string, stateDir: string, applicationDirectory: string) {
  const target = absoluteDirectory(request.target);
  await rejectDirectoryLinks(target);
  for (const reserved of [stateDir, applicationDirectory]) {
    if (related(reserved, target) || related(target, reserved)) throw new StorageError('reserved_directory', '请选择与程序及启动配置分开的专用数据文件夹。');
  }
  const sameCurrent = currentRoot && path.resolve(currentRoot).toLowerCase() === path.resolve(target).toLowerCase();
  if (currentRoot && (related(currentRoot, target) || related(target, currentRoot)) && !(request.action === 'open' && sameCurrent)) throw new StorageError('overlapping_directory', '迁移目标不能与当前目录相同，也不能互相包含。');
  if (request.backup) await rejectDirectoryLinks(absoluteDirectory(request.backup));
}
export function uniqueArchive(parent: string, action: string) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return path.join(parent, `reception-${action}-${stamp}-${randomUUID().slice(0, 8)}`);
}
export function maintenanceArguments(action: string, source: string, options: { target?: string; archive?: string; backup?: string; mode: 'demo' | 'live' }) {
  const args = ['--maintenance', action, '--source', source, '--mode', options.mode];
  if (options.target) args.push('--destination', options.target);
  if (options.archive) args.push('--archive', options.archive);
  if (options.backup) args.push('--backup-dir', options.backup);
  return args;
}
/** Offline command has no renderer input beyond validated paths and never invokes service RPC. */
export function runMaintenance(command: MaintenanceCommand, args: string[]): Promise<MaintenanceResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command.executable, [...command.prefix, ...args], { cwd: command.cwd, env: maintenanceEnvironment(command.env), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; let oversized = false;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => { if (output.length + chunk.length > 1024 * 1024) oversized = true; else output += chunk; });
    child.stderr.on('data', () => { /* Never expose arbitrary native output. */ });
    child.once('error', () => reject(new StorageError('maintenance_unavailable', '离线数据工具未能启动，请检查程序组件。')));
    child.once('close', code => {
      if (oversized) { reject(new StorageError('invalid_maintenance_result', '数据工具返回超过限制，目录选择未更改。')); return; }
      let result: Record<string, unknown>;
      try { result = JSON.parse(output.trim()); } catch { reject(new StorageError('invalid_maintenance_result', '未收到完整的数据操作结果，目录选择未更改。')); return; }
      if (!result || typeof result !== 'object' || Array.isArray(result)) { reject(new StorageError('invalid_maintenance_result', '数据操作结果无效。')); return; }
      if (code !== 0 || result.status !== 'completed') {
        const knownCode = typeof result.code === 'string' && /^[a-z_]{1,80}$/.test(result.code) ? result.code : 'storage_refused';
        const message = Object.hasOwn(maintenanceMessages, knownCode) ? maintenanceMessages[knownCode] : '离线校验未通过，请保留原数据和恢复信息并核查目录状态。';
        reject(new StorageError(knownCode, message)); return;
      }
      const optionalPath = (value: unknown) => value === null || typeof value === 'string' && path.isAbsolute(value) && !/[\u0000-\u001f]/.test(value);
      if (typeof result.data_root !== 'string' || !path.isAbsolute(result.data_root) || /[\u0000-\u001f]/.test(result.data_root) || typeof result.action !== 'string' || !optionalPath(result.backup) || result.recovery_backup !== undefined && !optionalPath(result.recovery_backup)) { reject(new StorageError('invalid_maintenance_result', '离线核验结果缺少必要信息，未切换目录。')); return; }
      resolve(result as unknown as MaintenanceResult);
    });
  });
}
export async function acquireStorageOperation(stateDir: string): Promise<() => Promise<void>> {
  await createArchiveParent(stateDir);
  if (process.platform !== 'win32') throw new StorageError('unsupported_platform', '目录切换功能目前仅支持Windows桌面版。');
  // A kernel mutex is released on process death; a wx file would strand startup after a crash.
  const name = 'Global\\ReceptionStorage_' + createHash('sha256').update(path.resolve(stateDir).toLowerCase()).digest('hex');
  const program = `$ErrorActionPreference='Stop'; $mutex=[Threading.Mutex]::new($false,'${name}'); $owned=$false; try { try { $owned=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned=$true }; if(-not $owned){[Console]::Out.WriteLine('busy');exit 2}; [Console]::Out.WriteLine('acquired'); [Console]::Out.Flush(); [void][Console]::ReadLine() } finally { if($owned){$mutex.ReleaseMutex()};$mutex.Dispose() }`;
  const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  child.stdin.on('error', () => {}); child.stderr.on('data', () => {});
  let exited = false;
  const exit = new Promise<void>(resolve => child.once('close', () => { exited = true; resolve(); }));
  await new Promise<void>((resolve, reject) => {
    let ready = false; let output = '';
    const timer = setTimeout(() => { child.stdin.end(); reject(new StorageError('storage_lock_unavailable', '数据管理锁未就绪，请稍后重新打开工作台。')); }, 15000);
    child.once('error', () => { clearTimeout(timer); reject(new StorageError('storage_lock_unavailable', 'Windows数据管理锁未能启动。')); });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      output += chunk;
      if (ready || !output.includes('\n')) return;
      ready = true; clearTimeout(timer);
      if (output.trim() === 'acquired') resolve();
      else { child.stdin.end(); reject(new StorageError('storage_operation_busy', '另一个数据管理操作尚未完成，请先核查原操作。')); }
    });
    child.once('close', () => { clearTimeout(timer); if (!ready) reject(new StorageError('storage_lock_unavailable', 'Windows数据管理锁未能取得，目录选择保持不变。')); });
  });
  let released = false;
  return async () => { if (released) return; released = true; if (!exited) child.stdin.end('\n'); await exit; };
}
export async function createArchiveParent(parent: string) {
  await rejectDirectoryLinks(parent);
  await mkdir(parent, { recursive: true });
}
