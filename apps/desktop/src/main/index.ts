import { createHash } from 'node:crypto';
import { app, BrowserWindow, clipboard, ClipboardItem, dialog, ipcMain, Menu, screen, shell, type IpcMainInvokeEvent, type WebFrameMain } from 'electron';
import { productUrl, readOriginal, validateReference } from './media-host';
import { SUPPLEMENT_MAX_FILE_BYTES } from '../shared/media-limits';
import { copyProductText } from './clipboard-host';
import { createWindowsWhatsAppMerchantBroker } from './whatsapp-merchant-broker';
import type { WhatsAppMerchantContext } from '../shared/whatsapp-account';
import { randomUUID } from 'node:crypto';
import { runLauncher } from './launcher-host';
import { createLauncherRegistry } from './launcher-registry';
import { createWindowsLauncherActivation } from './launcher-activation';
import { createModelSettingsStore } from './model-settings-store';
import { ModelConfigHost } from './model-config-host';
import { modelBudgetEnvironment } from './model-budget-environment';
import { retireLegacyTranslation } from './translation-retirement';
import { createExitDialogHost } from './exit-dialog-host';
import { FeishuCredentialStore } from './feishu-credentials';
import { FeishuConfigHost } from './feishu-config-host';
import { createWindowsUserEncryption } from './windows-user-encryption';
import path from 'node:path';
import { existsSync, mkdirSync } from 'node:fs';
import { mkdir, readFile, writeFile, rename, lstat } from 'node:fs/promises';
import { receivePaths, receiveUploads, validateConversation, windowsClipboardPaths } from './supplement-host';
import type { Supplement, SupplementUpload } from '../shared/api';
import { isWorkbenchPage } from './trusted-page';
import { API_METHODS, type Mode, type Preferences, type Snapshot } from '../shared/api';
import { ServiceClient, BackendError } from './service-client';
import { buildAccioLaunchInfo } from './launch-command';
import type { AccioLaunchInfo } from '../shared/launch';
import { UpdateHost } from './update-host';
import { updateSourceFromConfig } from '../../../../scripts/update-source.mjs';
import { installedDistribution, startUpdateRestart, INSTALL_APP_ID } from './update-restart';
import { version as programVersion } from '../../../../package.json';
import { validateChatParameters } from '../shared/chat-protocol';
import type { ApiMethod, StorageInfo, StorageRequest, StorageResult } from '../shared/api';
import { getBootstrapPaths, readBootstrap, writeBootstrap, resolveDataLocation } from '../../../../scripts/data-location.mjs';
import { cleanStorageRequest, validateStorageDestination, rejectDirectoryLinks, uniqueArchive, createArchiveParent, acquireStorageOperation, runMaintenance, maintenanceArguments, safetyBackupParent, StorageError, type MaintenanceCommand } from './storage-host';
import { spawn } from 'node:child_process';
import { consumeAutoLaunchContext } from '../../../../scripts/merchant-profiles.mjs';
import { acquireInstallationStartGuard } from './installation-guard';
import { registeredNewRoot, workbenchRestartArguments, launchErrorMessage, genericLaunchMessage } from './launch-safety';

class DispatchError extends Error {}

const launchArgs = process.argv.slice(1);
async function dispatchAuthorizedLaunch(): Promise<void> {
  const runtime = process.env.ACCIO_NODE_BIN;
  if (!runtime || !path.isAbsolute(runtime)) throw new Error('授权运行环境不可用。');
  const root = app.isPackaged ? path.dirname(process.execPath) : path.resolve(__dirname, '../../..');
  const script = path.join(root, 'scripts', 'connect-live.mjs');
  const forwarded: string[] = ['--current-executable', process.execPath];
  if (!app.isPackaged) forwarded.push('--development-entry', path.resolve(process.argv[1]));
  const constraints = new Map<string, string>();
  for (let index = 0; index < launchArgs.length; index++) {
    const match = /^(--expect-seller|--seller-id|--data-dir|--workctl)(?:=(.*))?$/.exec(launchArgs[index]);
    if (!match) continue;
    const name = match[1] === '--seller-id' ? '--expect-seller' : match[1];
    const value = match[2] === undefined ? launchArgs[++index] : match[2];
    if (constraints.has(name) || !value || value.startsWith('--') || /[\u0000\r\n]/.test(value)) throw new Error('启动参数无效。');
    constraints.set(name, value);
  }
  for (const [name, value] of constraints) forwarded.push(name, value);
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(runtime, [script, ...forwarded], { cwd: root, env, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  const chunks: Buffer[] = []; let size = 0; let overflow = false;
  child.stderr.on('data', (chunk: Buffer) => {
    size += chunk.length;
    if (size <= 16384) chunks.push(chunk); else { overflow = true; chunks.length = 0; }
  });
  await new Promise<void>((resolve, reject) => {
    child.once('error', reject);
    child.once('close', code => code === 0 ? resolve() : reject(new DispatchError(launchErrorMessage(Buffer.concat(chunks), overflow))));
  });
}

async function start(): Promise<void> {
  try {
    const context = consumeAutoLaunchContext(process.env, launchArgs);
    const authorizedRequest = launchArgs.includes('--connect-from-accio') || launchArgs.includes('--live') && !!process.env.ACCIO_GATEWAY_TOKEN;
    if (authorizedRequest && !context) {
      await dispatchAuthorizedLaunch();
      app.exit(0);
      return;
    }
    if (context) runWorkbench();
    else await runLauncher();
  } catch (error) {
    await app.whenReady();
    dialog.showErrorBox('工作台未能启动', error instanceof DispatchError ? error.message : genericLaunchMessage);
    app.exit(2);
  }
}

function runWorkbench(): void {
const args = launchArgs;
function option(name: string) { const index=args.indexOf(name); return index>=0 && typeof args[index+1]==='string' ? args[index+1] : ''; }
const mode: Mode = app.isPackaged || args.includes('--live') || args.includes('--connect-from-accio') ? 'live' : 'demo';
const expectedSeller = option('--seller-id') || option('--expect-seller');
if (expectedSeller && !/^[1-9]\d*$/.test(expectedSeller)) throw new Error('Expected seller ID must use positive ASCII digits.');
const bootstrapPaths = getBootstrapPaths();
let startupError = '';
let startupRecoveryBackup: string | null = null;
let location: ReturnType<typeof resolveDataLocation>;
try { location = resolveDataLocation(args); }
catch (error) {
  startupError = '数据目录配置不可用，请在连接与设置中核查已保存的目录。';
  location = { currentRoot: '', defaultRoot: bootstrapPaths.defaultRoot, configuredRoot: null, configFile: bootstrapPaths.configFile, source: 'default' };
}
const dataRoot = location.currentRoot;
const projectRoot = app.isPackaged ? app.getAppPath() : path.resolve(__dirname, '../../..');
const rendererFile = path.join(__dirname, '../renderer/index.html');
// One live owner per data root: changing expected seller must not create a
// second writer for the same merchant database beside an existing live window.
let profileRoot = dataRoot ? path.join(dataRoot, 'host', mode, mode === 'live' ? 'default' : expectedSeller || 'default') : path.join(bootstrapPaths.stateDir, 'recovery-host', mode);
app.setName('询盘接待助手');
try { mkdirSync(profileRoot, { recursive: true }); }
catch {
  startupError = '数据目录不可访问。已停止业务服务，请在连接与设置中核查目录或打开已有工作区。';
  profileRoot = path.join(bootstrapPaths.stateDir, 'recovery-host', mode);
  mkdirSync(profileRoot, { recursive: true });
}
app.setPath('userData', profileRoot);
let window: BrowserWindow | null = null;
let backend: ServiceClient | null = null;
let backendStartPromise: Promise<void> | null = null;
let identityTitlePending = false;
let identityTitleReady = false;
let refreshLauncherCompanyName: (() => void) | null = null;
let closing = false;
let canDestroy = false;
let receptionIntent = 0;
let preferences: Preferences = { nav_preference:'expanded', details_preference:'expanded', list_width:280, details_width:334 };
let preferenceWrites = Promise.resolve();
let storageBusy = false;
let storageTask: StorageRequest | null = null;
let maintenanceReadyToQuit = false;
let updateTask = false;
let updatePrepared = false;
let updateRecoveryStarted = false;
let cancelUpdateRestart: (() => Promise<void>) | null = null;
let updateHostPromise: Promise<UpdateHost> | null = null;
const imageRequests = new Map<string, AbortController>();
const usedImageRequestIds = new Set<string>();
const imageCopies = new Set<AbortController>();
let closeActivation: (() => Promise<void>) | null = null;
let closeMerchantContext: (() => Promise<void>) | null = null;
function abortImages() { for (const controller of imageRequests.values()) controller.abort(); for (const controller of imageCopies) controller.abort(); }
async function revokeActivation() { const close = closeActivation, context = closeMerchantContext; closeActivation = closeMerchantContext = null; await Promise.all([close?.(), context?.()]); }
const methods = new Set<string>(API_METHODS);
let modelSyncTimer: ReturnType<typeof setInterval> | null = null;
let feishuConfigHost: FeishuConfigHost | null = null;
let feishuSyncPending = false;
function feishuHost(): FeishuConfigHost {
  const ready = () => { if (!backend || closing || storageBusy || updateTask || backendStartPromise) throw new Error('工作区暂不可修改飞书配置。'); };
  return feishuConfigHost ??= new FeishuConfigHost(new FeishuCredentialStore(path.join(bootstrapPaths.stateDir, 'feishu-credentials', mode), createWindowsUserEncryption()), call, mode, dataRoot, ready);
}
function synchronizeFeishu(): void {
  if (!backend || closing || storageBusy || updateTask || backendStartPromise || feishuSyncPending) return;
  feishuSyncPending = true;
  void feishuHost().synchronize().catch(() => {}).finally(() => { feishuSyncPending = false; });
}
const exitDialogHost = createExitDialogHost<BrowserWindow, WebFrameMain, IpcMainInvokeEvent>({
  currentFrame: target => target.isDestroyed() || target.webContents.isDestroyed() ? null : target.webContents.mainFrame,
  isSource: (event, target, frame) => {
    try { validSender(event); return target === window && event.sender === target.webContents && event.senderFrame === frame && target.webContents.mainFrame === frame; }
    catch { return false; }
  },
  send: (target, event) => { if (target.isDestroyed() || target.webContents.isDestroyed()) throw new Error('窗口已关闭。'); target.webContents.send('reception:exit-dialog', event); },
});

let confirmationDetails: import('../shared/confirmation-dialog').ConfirmationDetails | null = null;
const confirmationDialogHost = createExitDialogHost<BrowserWindow, WebFrameMain, IpcMainInvokeEvent>({
  currentFrame: target => target.isDestroyed() || target.webContents.isDestroyed() ? null : target.webContents.mainFrame,
  isSource: (event, target, frame) => {
    try { validSender(event); return target === window && event.sender === target.webContents && event.senderFrame === frame && target.webContents.mainFrame === frame; }
    catch { return false; }
  },
  send: (target, event) => { if (target.isDestroyed() || target.webContents.isDestroyed()) throw new Error('窗口已关闭。'); target.webContents.send('reception:confirmation-dialog', { ...event, ...confirmationDetails }); },
});

function validSender(event: IpcMainInvokeEvent) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) throw new Error('无效的操作来源。');
  if (!isWorkbenchPage(event.senderFrame.url, rendererFile)) throw new Error('当前页面不允许访问接待服务。');
}
function parameters(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value) || JSON.stringify(value).length > 262144) throw new Error('操作参数无效或超过限制。');
  return JSON.parse(JSON.stringify(value));
}
async function call<T = unknown>(method: string, params: Record<string,unknown> = {}): Promise<T> {
  if (!backend || closing) throw new BackendError('backend_unavailable','后台未就绪或正在退出，请保留编辑内容。');
  return backend.invoke<T>(method, params);
}
function updateVerifiedTitle(): void {
  if (mode !== 'live' || closing || identityTitleReady || identityTitlePending || !backend) return;
  identityTitlePending = true;
  void call<Snapshot>('get_snapshot').then(snapshot => {
    if (!closing && window && !window.isDestroyed() && snapshot.app.account_verified && /^[1-9]\d*$/.test(snapshot.app.seller_id) && (!expectedSeller || expectedSeller === snapshot.app.seller_id)) {
      window.setTitle(`询盘接待助手 · 商家 ${snapshot.app.seller_id}`);
      identityTitleReady = true;
    }
  }).catch(() => {}).finally(() => { identityTitlePending = false; });
}
async function confirm(title: string, message: string, detail: string, action: string) {
  if (!window || window.isDestroyed()) return false;
  const details = { title, message, detail, action };
  confirmationDetails = details;
  try { return await confirmationDialogHost.request(window) === 'save'; }
  finally { if (confirmationDetails === details) confirmationDetails = null; }
}
async function accioLaunchInfo(): Promise<AccioLaunchInfo> {
  if (closing || storageBusy || updateTask) throw new Error('工作台正在退出或管理数据，请稍后复制启动指令。');
  const distribution = !app.isPackaged ? 'development' : await installedDistribution(process.resourcesPath, programVersion) ? 'installed' : 'portable';
  return buildAccioLaunchInfo({ version: programVersion, distribution, executable: process.execPath,
    developmentEntry: !app.isPackaged ? path.resolve(process.argv[1]) : undefined });
}
async function copyAccioLaunchInstructions(): Promise<AccioLaunchInfo> {
  const info = await accioLaunchInfo();
  try { await clipboard.writeText(info.instructions); }
  catch { throw new Error('复制失败，请在指令详情中手动选择并复制。'); }
  return info;
}
async function connectionInstructions() {
  if (!window) return;
  try {
    const info = await accioLaunchInfo();
    const response = await dialog.showMessageBox(window, { type: 'info', title: '连接 Accio', message: '从当前授权会话启动', detail: info.instructions,
      buttons: ['复制启动指令', '关闭'], defaultId: 1, cancelId: 1, noLink: true });
    if (response.response === 0) await copyAccioLaunchInstructions();
  } catch {
    await dialog.showMessageBox(window, { type: 'info', title: '启动指令暂不可用', message: '请先在连接与设置中核查数据位置，待退出或数据管理结束后重试。', buttons: ['知道了'] });
  }
}
function storageInfo(): StorageInfo {
  let config: ReturnType<typeof readBootstrap> = { version: 1 };
  let error = startupError;
  try { config = readBootstrap(bootstrapPaths.configFile); } catch { error ||= '目录配置无法读取，请核查后重新选择已有工作区。'; }
  return { current_root: dataRoot, default_root: bootstrapPaths.defaultRoot, configured_root: config.dataRoot || null, source: config.dataRoot && dataRoot && path.resolve(config.dataRoot).toLowerCase() === path.resolve(dataRoot).toLowerCase() ? 'configured' : location.source, config_file: bootstrapPaths.configFile, busy: storageBusy || updateTask || !!backendStartPromise,
    last_backup: config.lastBackup ? { path: config.lastBackup.path, completed_at: config.lastBackup.completedAt } : null,
    recovery_backup: startupRecoveryBackup,
    last_result: config.lastResult ? { action: config.lastResult.action, status: config.lastResult.status, message: config.lastResult.message, completed_at: config.lastResult.completedAt } : null,
    ...(error ? { error } : {}) };
}
function storageProgress(stage: string, message: string) {
  if (window && !window.isDestroyed()) window.webContents.send('reception:event', { event: 'storage.progress', data: { stage, message } });
}
const maintenanceProgress = { onProgress: (stage: string) => storageProgress(stage, stage === 'cancel_requested' ? '已请求停止维护，正在等待数据工具实际退出，请勿关闭程序。' : '正在维护数据，请等待操作完成。') };
async function backendCommand(): Promise<MaintenanceCommand> {
  let executable: string; let prefix: string[];
  if (app.isPackaged) { executable = path.join(process.resourcesPath, 'backend', 'reception-backend.exe'); prefix = []; }
  else {
    const candidates = [process.env.RECEPTION_PYTHON, path.join(projectRoot, 'services/reception/.venv/Scripts/python.exe'), path.join(projectRoot, 'services/reception/.venv/bin/python')];
    executable = candidates.find(value => value && existsSync(value)) || '';
    prefix = ['-I', '-u', path.join(projectRoot, 'services/reception/run.py')];
  }
  if (!executable || !existsSync(executable)) throw new StorageError('backend_missing', '后台运行环境或组件缺失。');
  return { executable, prefix, cwd: app.isPackaged ? process.resourcesPath : projectRoot, env: { ...await modelBudgetEnvironment(process.env), PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' } };
}
async function performStorageTask(request: StorageRequest): Promise<string> {
  let captured: ReturnType<typeof readBootstrap>;
  let damagedConfig: Buffer | undefined;
  try { captured = readBootstrap(bootstrapPaths.configFile); }
  catch {
    if (request.action !== 'open' || !startupError) throw new StorageError('config_unavailable', '目录配置无法读取；请先核查原配置。');
    await rejectDirectoryLinks(bootstrapPaths.configFile);
    const info = await lstat(bootstrapPaths.configFile);
    if (!info.isFile() || info.size > 65536) throw new StorageError('config_unavailable', '配置文件异常，已保留原文件，请联系维护人员。');
    damagedConfig = await readFile(bootstrapPaths.configFile);
    captured = { version: 1 };
  }
  const safetyParent = safetyBackupParent(bootstrapPaths.stateDir, process.env.RECEPTION_MERCHANT_REGISTRY_DIR, [dataRoot, app.isPackaged ? path.dirname(process.execPath) : projectRoot]);
  await createArchiveParent(safetyParent);
  let result;
  if (request.action === 'backup') {
    result = await runMaintenance(await backendCommand(), maintenanceArguments('backup', dataRoot, { mode, target: uniqueArchive(request.target!, 'backup') }), maintenanceProgress);
  } else if (request.action === 'open') {
    const historyRoot = dataRoot || captured.dataRoot;
    const sameRoot = historyRoot && path.resolve(historyRoot).toLowerCase() === path.resolve(request.target!).toLowerCase();
    result = await runMaintenance(await backendCommand(), sameRoot ? maintenanceArguments('inspect', historyRoot, { mode }) : maintenanceArguments('open', historyRoot || request.target!, { mode, ...(historyRoot ? { target: request.target, backup: safetyParent } : {}) }), maintenanceProgress);
  } else {
    result = await runMaintenance(await backendCommand(), maintenanceArguments(request.action, dataRoot, { mode, target: request.target, archive: request.backup, backup: safetyParent }), maintenanceProgress);
  }
  const nextRoot = request.action === 'backup' ? dataRoot : request.target!;
  if (path.resolve(result.data_root) !== path.resolve(nextRoot)) throw new StorageError('target_mismatch', '核验结果与选择的目录不一致，未切换。');
  const completedAt = Date.now();
  const previousRoot = dataRoot || captured.dataRoot;
  const next = { ...captured, ...(request.action === 'backup' ? {} : { dataRoot: nextRoot, ...(previousRoot ? { previousRoot } : {}) }),
    ...(result.backup ? { lastBackup: { path: result.backup, completedAt } } : {}),
    lastResult: { action: request.action, status: 'completed', message: request.action === 'backup' ? '完整备份已通过校验。' : '数据目录已核验并切换，自动接待保持暂停。', completedAt } };
  if (damagedConfig) {
    if (!(await readFile(bootstrapPaths.configFile)).equals(damagedConfig)) throw new StorageError('config_changed', '目录配置已被其他操作更改，请重新核查。');
    const preserved = path.join(bootstrapPaths.stateDir, `storage-invalid-${Date.now()}.json`);
    if (existsSync(preserved)) throw new StorageError('config_changed', '配置保留文件已存在，请重新核查。');
    await rename(bootstrapPaths.configFile, preserved);
    try { writeBootstrap(bootstrapPaths.configFile, next); }
    catch (error) { if (!existsSync(bootstrapPaths.configFile)) await rename(preserved, bootstrapPaths.configFile); throw error; }
  } else writeBootstrap(bootstrapPaths.configFile, next, captured);
  return nextRoot;
}
async function manageStorage(input: unknown, confirmedInPage = false): Promise<StorageResult> {
  if (storageBusy || updateTask || closing || backendStartPromise) return { status: 'failed', message: '后台正在启动、退出、升级或执行数据管理，请等待完成。' };
  let request: StorageRequest;
  try {
    request = cleanStorageRequest(input);
    if ((!dataRoot || startupError) && request.action !== 'open') return { status: 'failed', message: '当前目录不可用；请先选择一个已存在的完整工作区。' };
    await validateStorageDestination(request, dataRoot, bootstrapPaths.stateDir, path.dirname(process.execPath));
  } catch (error) { return { status: 'failed', message: error instanceof StorageError ? error.message : '数据目录校验未通过，请检查完整路径和访问权限。' }; }
  if (storageBusy || updateTask || closing) return { status: 'failed', message: '已有升级或数据管理操作正在进行。' };
  storageBusy = true;
  abortImages();
  let release: (() => Promise<void>) | undefined;
  try {
    release = await acquireStorageOperation(bootstrapPaths.stateDir);
    const names = { migrate: '迁移当前数据', open: '打开已有工作区', backup: '完整备份', restore: '恢复备份' };
    const accepted = confirmedInPage || await confirm('数据与备份', `${names[request.action]}并重启工作台？`, `当前目录：${dataRoot || '未连接'}\n目标目录：${request.target}\n${request.backup ? `备份来源：${request.backup}\n` : ''}将先保存编辑、停止后台并核验数据。原目录保留；非空迁移目标和缺少较新发送账本的旧备份会被拒绝。重启后自动接待保持暂停。`, '保存并继续');
    if (!accepted) return { status: 'cancelled', message: '已取消，数据目录保持不变。' };
    storageTask = request;
    const restarted = await closeWindow();
    return restarted ? { status: 'restarting', message: '正在重启工作台，自动接待保持暂停。' } : { status: 'cancelled', message: '请先处理未保存编辑，数据目录保持不变。' };
  } catch (error) {
    return { status: 'failed', message: error instanceof StorageError ? error.message : '数据操作未完成，原工作区和目录选择已保留。' };
  } finally {
    storageTask = null; storageBusy = false;
    await release?.();
    if (maintenanceReadyToQuit) { canDestroy = true; window?.destroy(); app.quit(); }
  }
}
async function getUpdateHost(): Promise<UpdateHost> {
  if (!updateHostPromise) updateHostPromise = (async () => {
    let trustedPublicKey = ''; let defaultFeedUrl = '';
    if (app.isPackaged) {
      try {
        const policyFile = path.join(process.resourcesPath, 'update-policy.json');
        const stat = await lstat(policyFile);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16384) throw new Error('invalid_policy');
        const policy = JSON.parse(await readFile(policyFile, 'utf8'));
        if (policy?.format === 1 && policy.app_id === INSTALL_APP_ID && typeof policy.public_key === 'string') trustedPublicKey = policy.public_key;
        const updateConfig = JSON.parse(await readFile(path.join(process.resourcesPath, 'app-update.yml'), 'utf8'));
        defaultFeedUrl = updateSourceFromConfig(updateConfig);
      } catch { /* No verified release policy means updates remain unavailable. */ }
    }
    return new UpdateHost({
      currentVersion: programVersion,
      installed: app.isPackaged && await installedDistribution(process.resourcesPath, programVersion),
      settingsFile: path.join(bootstrapPaths.stateDir, 'updates', 'settings.json'),
      trustedPublicKey, defaultFeedUrl,
      onChange: info => { if (window && !window.isDestroyed()) window.webContents.send('reception:event', { event: 'updates.changed', data: info }); },
      beforeInstall: prepareUpdateInstall,
      completeInstall: () => { if (!updatePrepared) throw new Error('升级准备尚未完成。'); canDestroy = true; window?.destroy(); app.quit(); },
      onInstallError: reopenAfterUpdateFailure,
    });
  })();
  return updateHostPromise;
}
function updateOperationReady() {
  if (closing || updateTask || storageBusy || backendStartPromise) throw new BackendError('update_busy', '后台正在启动、退出或管理数据，请等待完成。');
}
async function reopenAfterUpdateFailure(): Promise<void> {
  await cancelUpdateRestart?.(); cancelUpdateRestart = null;
  if (!closing) { updatePrepared = false; updateTask = false; return; }
  if (updateRecoveryStarted) return;
  updateRecoveryStarted = true;
  // A stopped service is reopened in a new process; old UI confirmations never revive.
  if (window && !window.isDestroyed()) await dialog.showMessageBox(window, { type: 'error', title: '升级尚未完成', message: '尚未完成升级，将重新打开原工作台。', detail: '原数据和已有备份保留。自动接待保持暂停，请检查连接和原操作结果后再恢复。', buttons: ['重新打开'] });
  app.relaunch({ args: workbenchRestartArguments(args, dataRoot) });
  canDestroy = true; window?.destroy(); app.quit();
}
async function prepareUpdateInstall(version: string, installerPath: string, installerSha512: string): Promise<boolean> {
  updateOperationReady();
  if (!window || !backend || !dataRoot || startupError) throw new Error('当前工作区尚未就绪，不能安装升级。');
  const target = window;
  updateTask = true;
  abortImages();
  let frozen = false;
  let gate: { commit: () => Promise<void>; cancel: () => Promise<void> } | null = null;
  try {
    const accepted = await confirm('安装更新', `安装 ${version} 并重启工作台？`, '将先保存编辑、暂停接待、等待在途结果并制作完整数据备份，再安装新版本。将继续使用当前数据目录；重启后自动接待保持暂停，Accio身份和读取重新核验。', '保存并安装');
    if (!accepted) return false;
    target.setEnabled(false); frozen = true;
    if (!(await savePendingEdits(target))) return false;
    await call<Snapshot>('get_snapshot');
    const restartArgs = args.includes('--connect-from-accio')
      ? workbenchRestartArguments(['--connect-from-accio', '--expect-seller', expectedSeller], dataRoot)
      : workbenchRestartArguments(args, dataRoot).filter(arg => arg !== process.argv[1]);
    // The helper acquires and retains the same data-management mutex through installation.
    gate = await startUpdateRestart({ executable: process.execPath, version, previousVersion: programVersion, installerPath, installerSha512, stateDirectory: bootstrapPaths.stateDir, parentPid: process.pid, args: restartArgs, resultFile: path.join(bootstrapPaths.stateDir, 'updates', 'restart-result.json') });
    cancelUpdateRestart = gate.cancel;
    const captured = readBootstrap(bootstrapPaths.configFile);
    if ((captured.dataRoot || null) !== (location.configuredRoot || null)) throw new Error('数据目录选择已改变，请重新打开工作台后升级。');
    await stopBackendForClose(target, false);
    const parent = safetyBackupParent(bootstrapPaths.stateDir, process.env.RECEPTION_MERCHANT_REGISTRY_DIR, [dataRoot, app.isPackaged ? path.dirname(process.execPath) : projectRoot]);
    await createArchiveParent(parent);
    const result = await runMaintenance(await backendCommand(), maintenanceArguments('backup', dataRoot, { mode, target: uniqueArchive(parent, 'pre-update') }), maintenanceProgress);
    if (!result.backup || path.resolve(result.data_root).toLowerCase() !== path.resolve(dataRoot).toLowerCase()) throw new Error('未收到完整备份确认。');
    writeBootstrap(bootstrapPaths.configFile, { ...captured, lastBackup: { path: result.backup, completedAt: Date.now() } }, captured);
    await gate.commit();
    updatePrepared = true;
    canDestroy = true;
    return true;
  } catch {
    await reopenAfterUpdateFailure();
    throw new Error('升级准备未完成，原数据和已有备份已保留。');
  } finally {
    if (!updatePrepared) {
      await gate?.cancel(); cancelUpdateRestart = null;
      updateTask = false;
      if (frozen && !closing && !target.isDestroyed()) target.setEnabled(true);
    }
  }
}
function cleanPreferences(value: Record<string,unknown>): Partial<Preferences> {
  const result:Partial<Preferences>={};
  if (value.nav_preference==='expanded'||value.nav_preference==='icons') result.nav_preference=value.nav_preference;
  if (value.details_preference==='expanded'||value.details_preference==='rail') result.details_preference=value.details_preference;
  if (typeof value.list_width==='number'&&Number.isFinite(value.list_width)) result.list_width=Math.max(240,Math.min(360,value.list_width));
  if (typeof value.details_width==='number'&&Number.isFinite(value.details_width)) result.details_width=Math.max(280,Math.min(600,value.details_width));
  if (typeof value.requirements_ratio==='number'&&Number.isFinite(value.requirements_ratio)) result.requirements_ratio=Math.max(0.1,Math.min(0.9,value.requirements_ratio));
  return result;
}
function installIPC() {
  const mediaReady = () => {
    if (!window || window.isDestroyed() || !backend || closing || storageBusy || updateTask || backendStartPromise) throw new Error('后台正在启动、退出或管理数据。');
  };
  const imageRequestId = (id: unknown): id is string => typeof id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id);
  ipcMain.handle('reception:image:original', async (event, reference: unknown, id: unknown) => {
    validSender(event); mediaReady();
    if (!imageRequestId(id) || usedImageRequestIds.has(id) || usedImageRequestIds.size >= 65536 || imageRequests.size >= 2) throw new Error('原图请求无效或繁忙。');
    usedImageRequestIds.add(id);
    const controller = new AbortController(); imageRequests.set(id, controller);
    try { return await readOriginal(call, reference, mediaReady, false, controller.signal); }
    finally { imageRequests.delete(id); }
  });
  ipcMain.handle('reception:image:cancel', (event, id: unknown) => {
    validSender(event);
    if (!imageRequestId(id)) throw new Error('原图请求无效。');
    imageRequests.get(id)?.abort();
  });
  const copyOriginal = async (reference: unknown) => {
    mediaReady(); validateReference(reference);
    const controller = new AbortController(); imageCopies.add(controller);
    try {
      await readOriginal(call, reference, mediaReady, true, controller.signal, async image => {
        mediaReady();
        await clipboard.write([new ClipboardItem({ 'image/png': new Blob([new Uint8Array(image.bytes)], { type: 'image/png' }) })]);
      });
    } finally { imageCopies.delete(controller); }
    return 'copied' as const;
  };
  ipcMain.handle('reception:image:copy', async (event, reference: unknown) => { validSender(event); await copyOriginal(reference); });
  ipcMain.handle('reception:image:menu', async (event, reference: unknown) => {
    validSender(event); mediaReady(); validateReference(reference);
    const target = window!;
    const selected = await new Promise<boolean>(resolve => {
      let chosen = false;
      const menu = Menu.buildFromTemplate([{ label: '复制图片', click: () => { chosen = true; } }]);
      menu.popup({ window: target, callback: () => resolve(chosen) });
    });
    return selected ? copyOriginal(reference) : 'cancelled';
  });
  ipcMain.handle('reception:product:open', async (event, reference: unknown) => {
    validSender(event); mediaReady();
    const url = await productUrl(call, reference, mediaReady);
    await shell.openExternal(url);
  });
  ipcMain.handle('reception:product:copy-text', async (event, reference: unknown) => {
    validSender(event); mediaReady();
    await copyProductText(call, reference, mediaReady, text => clipboard.writeText(text));
  });
  ipcMain.handle('reception:exit-dialog:respond', (event, value: unknown) => exitDialogHost.respond(event, value));
  ipcMain.handle('reception:confirmation-dialog:respond', (event, value: unknown) => confirmationDialogHost.respond(event, value));
  window?.on('closed', () => { exitDialogHost.cancel(); confirmationDialogHost.cancel(); });
  window?.webContents.on('did-start-navigation', () => { exitDialogHost.cancel(); confirmationDialogHost.cancel(); });
  ipcMain.handle('reception:feishu:save', async (event, input) => { validSender(event); return feishuHost().save(input); });
  ipcMain.handle('reception:feishu:grant', async (event, input) => { validSender(event); await feishuHost().synchronize(); return feishuHost().grant(input); });
  ipcMain.handle('reception:models:summary', async event => { validSender(event); return (await createModelSettingsStore()).read(); });
  const openLauncher = async (page: 'home' | 'models') => {
    const args = app.isPackaged ? [] : [path.resolve(process.argv[1])];
    args.push(`--launcher-page=${page}`);
    const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
    for (const key of Object.keys(env)) if (/^(ACCIO_|RECEPTION_)/i.test(key)) delete env[key];
    const child = spawn(process.execPath, args, { detached: true, stdio: 'ignore', env });
    await new Promise<void>((resolve, reject) => { child.once('error', reject); child.once('spawn', () => { child.unref(); resolve(); }); });
  };
  ipcMain.handle('reception:models:launcher', async event => { validSender(event); await openLauncher('models'); });
  ipcMain.handle('reception:launcher:open', async event => { validSender(event); await openLauncher('home'); });
  ipcMain.handle('reception:launch:get', async event => { validSender(event); return accioLaunchInfo(); });
  ipcMain.handle('reception:launch:copy', async event => { validSender(event); return copyAccioLaunchInstructions(); });
  ipcMain.handle('reception:update:get', async event => { validSender(event); return (await getUpdateHost()).getInfo(); });
  ipcMain.handle('reception:update:settings', async (event, input: unknown) => { validSender(event); updateOperationReady(); return (await getUpdateHost()).saveSettings(input); });
  ipcMain.handle('reception:update:check', async event => { validSender(event); updateOperationReady(); return (await getUpdateHost()).check(); });
  ipcMain.handle('reception:update:download', async event => { validSender(event); updateOperationReady(); return (await getUpdateHost()).download(); });
  ipcMain.handle('reception:update:install', async event => { validSender(event); updateOperationReady(); return (await getUpdateHost()).install(); });
  ipcMain.handle('reception:storage:get', event => { validSender(event); return storageInfo(); });
  ipcMain.handle('reception:storage:choose', async (event, purpose: unknown) => {
    validSender(event); if (!window || closing || storageBusy) return null;
    if (!['target', 'backup', 'restore'].includes(String(purpose))) throw new Error('目录选择用途无效。');
    const titles = { target: '选择数据目录', backup: '选择备份存放位置', restore: '选择包含manifest.json的完整备份目录' };
    const selected = await dialog.showOpenDialog(window, { title: titles[purpose as keyof typeof titles], properties: ['openDirectory', 'createDirectory'] });
    return selected.canceled ? null : selected.filePaths[0] || null;
  });
  ipcMain.handle('reception:storage:open', async (event, which: unknown) => {
    validSender(event); if (which !== 'current' && which !== 'backup') throw new Error('目录类型无效。');
    const info = storageInfo(); const folder = which === 'current' ? info.current_root : info.last_backup?.path;
    if (!folder || !existsSync(folder)) throw new Error('目录不存在或尚无成功备份。');
    const failure = await shell.openPath(folder); if (failure) throw new Error('未能打开目录，请检查访问权限。');
  });
  ipcMain.handle('reception:storage:manage', (event, request: unknown) => { validSender(event); return manageStorage(request, true); });
  ipcMain.handle('reception:invoke', async(event, method:unknown, input:unknown)=>{
    validSender(event);
    if (typeof method!=='string'||!methods.has(method)||method.includes('whatsapp')) throw new Error('不允许此操作。');
    const params=parameters(input??{});
    validateChatParameters(method as ApiMethod, params);
    if(method==='set_scope'||method==='save_settings')++receptionIntent;
    const intent = method==='set_reception' ? ++receptionIntent : null;
    if (mode==='live'&&method==='check_ai'&&!(await confirm('合成 AI 检测','执行一次合成 AI 检测？','会消耗一次 AI 生成，不发送客户消息或写飞书；失败不会自动重试。请先暂停接待。','开始一次检测'))) return { cancelled:true };
    if (mode==='live'&&method==='run_outbox'&&!(await confirm('同步飞书待办','执行待办同步？','仅处理本账号已经保存的需求同步任务；未知提交不会换键重发。','同步待办'))) return { cancelled:true };
    if (method==='set_reception' && params.enabled===true && intent!==receptionIntent) return call('get_snapshot');
    if (method === 'check_feishu') await feishuHost().synchronize();
    return call(method,params);
  });
  const supplementReady = () => {
    if (!window || !backend || closing || storageBusy || updateTask || backendStartPromise) throw new Error('后台正在启动、退出或管理数据，请等待完成。');
  };
  const supplementUpload = (conversationId: string) => (file: SupplementUpload) => call<Supplement>('upload_supplement_attachment', { conversation_id: conversationId, ...file });
  const chooseSupplements = async (conversationId: unknown, multiple: boolean) => {
    validateConversation(conversationId); supplementReady();
    const selected = await dialog.showOpenDialog(window!, { title: '选择业务补充附件', properties: multiple ? ['openFile', 'multiSelections'] : ['openFile'],
      filters: [{ name: '图片、PDF 和 UTF-8 文本', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif', 'pdf', 'txt', 'md', 'csv'] }] });
    if (selected.canceled || !selected.filePaths.length) return null;
    supplementReady();
    return receivePaths(selected.filePaths, supplementUpload(conversationId), supplementReady);
  };
  ipcMain.handle('reception:choose-supplement-attachment', async (event, conversationId: unknown) => {
    validSender(event);
    const result = await chooseSupplements(conversationId, false);
    if (result?.errors.length) throw new Error(result.errors[0]);
    return result?.supplement ?? null;
  });
  ipcMain.handle('reception:choose-supplement-attachments', async (event, conversationId: unknown) => {
    validSender(event); return chooseSupplements(conversationId, true);
  });
  ipcMain.handle('reception:upload-supplement-attachments', async (event, conversationId: unknown, files: unknown) => {
    validSender(event); validateConversation(conversationId); supplementReady();
    return receiveUploads(files, supplementUpload(conversationId), supplementReady);
  });
  ipcMain.handle('reception:paste-supplement-attachments', async (event, conversationId: unknown) => {
    validSender(event); validateConversation(conversationId); supplementReady();
    const paths = await windowsClipboardPaths();
    supplementReady();
    if (paths.length) return receivePaths(paths, supplementUpload(conversationId), supplementReady);
    const items = await clipboard.read();
    const image = items.find(item => item.types.includes('image/png'));
    if (!image) return { supplement: null, errors: [] };
    const blob = await image.getType('image/png');
    if (blob.size > SUPPLEMENT_MAX_FILE_BYTES) return { supplement: null, errors: ['单个附件不能超过 20 MiB。'] };
    supplementReady();
    const bytes = Buffer.from(await blob.arrayBuffer());
    return receiveUploads([{ name: '剪贴板图片.png', content_b64: bytes.toString('base64') }], supplementUpload(conversationId), supplementReady);
  });
  ipcMain.handle('reception:choose-workctl',async event=>{
    validSender(event); if (!window) return null;
    const value=await dialog.showOpenDialog(window,{title:'选择当前账号 workctl.exe',properties:['openFile'],filters:[{name:'workctl 可执行文件',extensions:['exe']}]});
    const selected=value.filePaths[0];
    if (value.canceled||!selected) return null;
    if (path.basename(selected).toLowerCase()!=='workctl.exe') throw new Error('请选择 workctl.exe。');
    return selected;
  });
  ipcMain.handle('reception:preferences:get',event=>{validSender(event);return preferences;});
  ipcMain.handle('reception:preferences:save',async(event,input:unknown)=>{
    validSender(event); preferences={...preferences,...cleanPreferences(parameters(input))};
    const captured={...preferences};
    preferenceWrites=preferenceWrites.catch(()=>{}).then(async()=>{
      await mkdir(profileRoot,{recursive:true});
      const temporary=path.join(profileRoot,'layout.json.tmp');
      await writeFile(temporary,JSON.stringify(captured),'utf8'); await rename(temporary,path.join(profileRoot,'layout.json'));
    });
    await preferenceWrites; return preferences;
  });
}
async function startBackend() {
  if (startupError || !dataRoot) return;
  const runner = await backendCommand();
  const release = await acquireStorageOperation(bootstrapPaths.stateDir);
  try {
    const refreshed = resolveDataLocation(args);
    if (path.resolve(refreshed.currentRoot) !== path.resolve(dataRoot) || refreshed.configuredRoot !== location.configuredRoot) throw new StorageError('config_changed', '启动期间数据目录已改变，请关闭后重新打开工作台。');
    const safetyParent = safetyBackupParent(bootstrapPaths.stateDir, process.env.RECEPTION_MERCHANT_REGISTRY_DIR, [dataRoot, app.isPackaged ? path.dirname(process.execPath) : projectRoot]);
    await createArchiveParent(safetyParent);
    const prepare = maintenanceArguments('prepare', dataRoot, { mode, backup: safetyParent });
    const sameConfigured = location.configuredRoot && path.resolve(location.configuredRoot).toLowerCase() === path.resolve(dataRoot).toLowerCase();
    const registeredNew = args.includes('--connect-from-accio') && sameConfigured && registeredNewRoot(process.env.RECEPTION_MERCHANT_REGISTRY_DIR, bootstrapPaths.stateDir, dataRoot, expectedSeller);
    if (location.source === 'default' || location.source === 'argument' && !sameConfigured || registeredNew) prepare.push('--allow-new');
    const prepared = await runMaintenance(runner, prepare, maintenanceProgress);
    // Raw recovery evidence has a different manifest and must not become a restore archive.
    startupRecoveryBackup = prepared.recovery_backup || null;
    if (prepared.backup) {
      const captured = readBootstrap(bootstrapPaths.configFile);
      writeBootstrap(bootstrapPaths.configFile, { ...captured, lastBackup: { path: prepared.backup, completedAt: Date.now() } }, captured);
    }
    await retireLegacyTranslation(bootstrapPaths.stateDir, mode);
    const command = [...runner.prefix, '--mode', mode, '--data-dir', dataRoot];
    if (expectedSeller) command.push('--seller-id', expectedSeller);
    if (option('--workctl')) command.push('--workctl', option('--workctl'));
    backend = new ServiceClient(runner.executable, command, runner.cwd, runner.env);
    backend.on('event', value => {
      // Independent WhatsApp business events never cross the merchant bridge.
      if (typeof value?.event === 'string' && value.event.startsWith('whatsapp.')) return;
      if (window && !window.isDestroyed()) window.webContents.send('reception:event', value);
      if (value?.event === 'state.changed') { updateVerifiedTitle(); refreshLauncherCompanyName?.(); synchronizeFeishu(); }
    });
    await backend.invoke('get_snapshot');
    const modelHost = new ModelConfigHost(await createModelSettingsStore(), (method, params) => call(method, params));
    let appliedModelRevision = (await modelHost.read()).directory_revision;
    await modelHost.synchronize();
    let syncingModels = false;
    modelSyncTimer = setInterval(() => {
      synchronizeFeishu();
      if (syncingModels || closing || !backend) return;
      syncingModels = true;
      void modelHost.read().then(async settings => {
        if (settings.directory_revision !== appliedModelRevision) {
          await modelHost.synchronize();
          appliedModelRevision = settings.directory_revision;
        }
      }).catch(() => { appliedModelRevision = -1; /* Authority checks in the backend fail closed until synchronization succeeds. */ }).finally(() => { syncingModels = false; });
    }, 3000);
    // Offline recovery may outlast the renderer's first snapshot request.
    // Announce readiness only after the new backend has answered successfully.
    if (window && !window.isDestroyed()) window.webContents.send('reception:event', { event: 'state.changed', data: { reason: 'backend_ready' } });
    updateVerifiedTitle();
  } finally { await release(); }
}
async function savePendingEdits(target: BrowserWindow): Promise<boolean> {
  let noUnsaved = false;
  try { noUnsaved = await target.webContents.executeJavaScript('window.dispatchEvent(new Event("beforeunload", {cancelable:true}))'); } catch { /* Keep an unavailable editor open. */ }
  if (noUnsaved) return true;
  let failed = false;
  while (!target.isDestroyed() && !target.webContents.isDestroyed()) {
    if (await exitDialogHost.request(target, failed ? 'save_failed' : undefined) !== 'save') return false;
    let saved = false;
    try { saved = await target.webContents.executeJavaScript('new Promise(resolve=>{const timer=setTimeout(()=>resolve(false),20000);window.dispatchEvent(new CustomEvent("reception:save-before-close",{detail:{finish:value=>{clearTimeout(timer);resolve(value===true);}}}));})'); } catch { /* Keep the editor and its current input. */ }
    if (saved) return true;
    failed = true;
  }
  return false;
}
async function stopBackendForClose(target: BrowserWindow, allowForce: boolean): Promise<void> {
  closing=true; ++receptionIntent;
  abortImages();
  await revokeActivation();
  if (modelSyncTimer) { clearInterval(modelSyncTimer); modelSyncTimer = null; }
    // Keep editing disabled through every wait/force-exit reminder after revocation.
  target.setEnabled(false);
  target.setTitle('询盘接待助手 · 正在停止后台并保存结果…');
  target.webContents.send('reception:event',{event:'backend.closing',data:{message:'正在撤销新发送并等待在途结果保存。'}});
  const stopped=(async () => {
    await Promise.all([
      backend?.invoke('set_reception', { enabled: false, auto_send: false }),
    ]);
    await backend?.shutdown();
  })();
  let done=false; void stopped.then(()=>{done=true;});
  while(!done) {
    let reminder: ReturnType<typeof setTimeout> | undefined;
    try { await Promise.race([stopped,new Promise(resolve=>{reminder=setTimeout(resolve,210000);})]); }
    finally { if(reminder)clearTimeout(reminder); }
    if (!done&&window&&!window.isDestroyed()) {
      const choice=await dialog.showMessageBox(window,{type:'warning',title:'后台操作仍在收尾',message:'仍在等待本次外部调用结束并保存结果。',detail:!allowForce ? '升级或数据管理必须等待正常关闭，当前尚未开始安装或切换数据。' : '继续等待可保留真实回执。强制退出后未决操作将需要核查，不会自动重发。',buttons:!allowForce ? ['继续等待'] : ['继续等待','强制退出'],defaultId:0,cancelId:0,noLink:true});
      target.setEnabled(false);
      if(allowForce && choice.response===1){backend?.forceExit();break;}
    }
  }
  await preferenceWrites.catch(()=>{});
}
async function closeWindow(): Promise<boolean> {
  if (backendStartPromise) await backendStartPromise.catch(() => {});
  if (closing || updateTask || !window || storageBusy && !storageTask) return false;
  const task = storageTask;
  const target = window;
  if (!(await savePendingEdits(target))) return false;
  await stopBackendForClose(target, !task);
  if (task) {
    let nextRoot = dataRoot;
    storageProgress('verify', '后台已正常结束，正在执行完整备份与目录核验…');
    try { nextRoot = await performStorageTask(task); }
    catch (error) {
      const message = error instanceof StorageError ? error.message : '数据操作未完成，原工作区和目录选择保持不变。';
      try {
        const captured = readBootstrap(bootstrapPaths.configFile);
        writeBootstrap(bootstrapPaths.configFile, { ...captured, lastResult: { action: task.action, status: 'failed', message, completedAt: Date.now() } }, captured);
      } catch { /* Preserve the original selection even when status cannot be saved. */ }
      await dialog.showMessageBox(target, { type: 'error', title: '数据操作未完成', message, detail: '已停止本次切换。将重新打开原工作区，自动接待保持暂停；请保留已有目录和备份。', buttons: ['重新打开原工作区'] });
    }
    storageProgress('restart', '核验已结束，正在重新打开工作台。自动接待不会自动恢复。');
    app.relaunch({ args: nextRoot ? workbenchRestartArguments(args, nextRoot) : args });
    maintenanceReadyToQuit = true;
    return true;
  }
  canDestroy=true; target.destroy(); app.quit(); return true;
}

if (!app.requestSingleInstanceLock()) { app.quit(); }
else {
  app.on('second-instance',(_event,commandLine)=>{if(window){if(window.isMinimized())window.restore();window.show();window.focus();if(commandLine.includes('--connect-from-accio'))void dialog.showMessageBox(window,{type:'info',title:'需要从授权会话重新启动',message:'当前工作区窗口已经运行。',detail:'授权环境不会注入已运行的进程。请先正常退出此窗口，再在 Accio 授权终端重新执行启动指令。',buttons:['知道了']});}});
  app.whenReady().then(async()=>{
    let releaseStart: () => Promise<void>;
    try { releaseStart = await acquireInstallationStartGuard(process.execPath); }
    catch (error) {
      if (error && typeof error === 'object' && 'code' in error && error.code === 'installation_busy') { app.exit(2); return; }
      throw error;
    }
    try {
    try { preferences={...preferences,...cleanPreferences(JSON.parse(await readFile(path.join(profileRoot,'layout.json'),'utf8')))}; } catch { /* first launch */ }
    const area=screen.getPrimaryDisplay().workArea;
    window=new BrowserWindow({width:Math.min(1440,Math.max(600,area.width-32)),height:Math.min(920,Math.max(400,area.height-32)),minWidth:Math.min(800,area.width),minHeight:Math.min(420,area.height),title:'询盘接待助手',backgroundColor:'#f7f8fa',show:false,autoHideMenuBar:true,webPreferences:{preload:path.join(__dirname,'../preload/index.js'),contextIsolation:true,nodeIntegration:false,sandbox:true,webSecurity:true}});
    window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    window.webContents.on('will-navigate',(event,url)=>{if(!isWorkbenchPage(url,rendererFile))event.preventDefault();});
    window.webContents.session.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
    installIPC();
    Menu.setApplicationMenu(Menu.buildFromTemplate([{label:'文件',submenu:[{label:'退出',click:()=>window?.close()}]},{label:'帮助',submenu:[{label:'复制 Accio 启动指令',accelerator:'CommandOrControl+Shift+L',click:()=>void connectionInstructions()},{label:'打开开发者工具',visible:!app.isPackaged,click:()=>window?.webContents.openDevTools()}]}]));
    window.on('close',event=>{if(!canDestroy){event.preventDefault();void closeWindow();}});
    window.once('ready-to-show',()=>window?.show());
    await window.loadFile(rendererFile);
    backendStartPromise = startBackend();
    try { await backendStartPromise; }
    catch (error) {
      startupError = error instanceof StorageError ? error.message : '数据目录预检未通过，请在连接与设置中核查数据位置。';
      storageProgress('failed', startupError);
      window.webContents.send('reception:event', { event: 'backend.disconnected', data: { message: startupError } });
    } finally { backendStartPromise = null; storageProgress('ready', startupError || '数据目录预检完成。'); }
    if (backend) window.webContents.send('reception:event', { event: 'state.changed', data: {} });
    if(args.includes('--connect-from-accio') && backend) {
      void (async () => { let verifiedConnection = false; try {
        const started = await call<{ job_id: string }>('check_connection');
        const deadline = Date.now() + 120000;
        let verifiedJob = false;
        while (Date.now() < deadline && backend && !closing) {
          const jobs = await call<Array<{job_id: string; kind: string; status: string}>>('list_jobs', { include_background: true });
          const job = jobs.find(item => item.job_id === started.job_id && item.kind === 'check_connection');
          if (job?.status === 'succeeded') { verifiedJob = true; break; }
          if (job && ['failed', 'cancelled', 'blocked'].includes(job.status)) break;
          await new Promise(resolve => setTimeout(resolve, 500));
        }
        if (!verifiedJob) throw new Error('connection_check_incomplete');
        const snapshot = await call<Snapshot>('get_snapshot');
        if (snapshot.app.account_verified && snapshot.app.seller_id === expectedSeller && /^[1-9]\d*$/.test(snapshot.app.seller_id)) {
          verifiedConnection = true;
          window?.setTitle(`询盘接待助手 · 商家 ${snapshot.app.seller_id}`);
          const registryDirectory = process.env.RECEPTION_MERCHANT_REGISTRY_DIR;
          if (registryDirectory && path.isAbsolute(registryDirectory) && path.isAbsolute(bootstrapPaths.stateDir) && path.isAbsolute(dataRoot)) {
            const refreshed = resolveDataLocation(args);
            if (path.resolve(refreshed.currentRoot) !== path.resolve(dataRoot)) throw new Error('data_root_changed');
            await Promise.all([registryDirectory, bootstrapPaths.stateDir, dataRoot].map(directory => rejectDirectoryLinks(directory)));
            const registry = await createLauncherRegistry();
            let record: Awaited<ReturnType<typeof registry.registerVerified>> | undefined;
            for (let attempt = 0; attempt < 3; attempt++) {
              const current = await call<Snapshot>('get_snapshot');
              if (closing || !current.app.account_verified || current.app.seller_id !== snapshot.app.seller_id || path.resolve(resolveDataLocation(args).currentRoot) !== path.resolve(dataRoot)) throw new Error('identity_changed');
              try { record = await registry.registerVerified({ seller_id: snapshot.app.seller_id, name: `商家 ${snapshot.app.seller_id.slice(-4)}`, registry_directory: path.resolve(registryDirectory), state_directory: path.resolve(bootstrapPaths.stateDir), data_root: path.resolve(dataRoot), verified_at: Date.now() }); break; }
              catch (error) { if (attempt === 2) throw error; await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1))); }
            }
            if (!record) throw new Error('launcher_registration_failed');
            const registered = record;
            let nameRefreshPending = false, nameRefreshAgain = false;
            refreshLauncherCompanyName = () => {
              nameRefreshAgain = true;
              if (nameRefreshPending) return;
              nameRefreshPending = true;
              void (async () => {
                while (nameRefreshAgain && !closing && !storageBusy && !updateTask) {
                  nameRefreshAgain = false;
                  const current = await call<Snapshot>('get_snapshot');
                  if (closing || storageBusy || updateTask || !current.app.account_verified || current.app.seller_id !== registered.seller_id || path.resolve(resolveDataLocation(args).currentRoot) !== registered.data_root) return;
                  await registry.updateCompanyName(registered, current.app.company_name);
                }
              })().catch(() => {}).finally(() => { nameRefreshPending = false; if (nameRefreshAgain && !closing && !storageBusy && !updateTask) refreshLauncherCompanyName?.(); });
            };
            refreshLauncherCompanyName();
            const latest = await call<Snapshot>('get_snapshot');
            if (closing || !latest.app.account_verified || latest.app.seller_id !== record.seller_id || path.resolve(resolveDataLocation(args).currentRoot) !== record.data_root) throw new Error('identity_changed');
            const activation = await createWindowsLauncherActivation();
            const validate = async () => {
              if (closing || storageBusy || updateTask || !window || window.isDestroyed()) throw new Error('window_unavailable');
              const current = await call<Snapshot>('get_snapshot');
              if (closing || storageBusy || updateTask || !current.app.account_verified || current.app.seller_id !== record.seller_id || path.resolve(resolveDataLocation(args).currentRoot) !== record.data_root || !window || window.isDestroyed()) throw new Error('identity_changed');
            };
            const close = await activation.publishVerifiedWindow(record, async () => {
              await validate();
              if (!window || window.isDestroyed()) throw new Error('window_unavailable');
              if (window.isMinimized()) window.restore(); window.show(); window.focus();
            }, validate);
            if (closing || storageBusy || updateTask) await close(); else closeActivation = close;
            const broker = await createWindowsWhatsAppMerchantBroker();
            const closeContext = await broker.publishVerifiedMerchant(record, async request => {
              const { request_id: _request, ...params } = request;
              return call<Pick<WhatsAppMerchantContext, 'seller_id' | 'data_root' | 'conversation_id' | 'revision' | 'context'>>('_wa_export_merchant_context', params);
            }, validate, async request => {
              const { request_id: _request, ...params } = request;
              return call<{ seller_id: string; data_root: string; candidates: Array<{ conversation_id: string; name: string; revision: string }> }>('_wa_export_customer_candidates', params);
            });
            if (closing || storageBusy || updateTask) await closeContext(); else closeMerchantContext = closeContext;
          } else throw new Error('launcher_registration_unavailable');
        }
      } catch {
        if (verifiedConnection && window && !window.isDestroyed() && !closing) void dialog.showMessageBox(window, { type: 'warning', title: '商家登记未完成', message: '平台读取已通过，但启动台登记或窗口连接未完成。', detail: '请检查本机目录权限后重新授权启动，或在启动台选择已有商家登记目录。', buttons: ['知道了'] });
      } })();
    }
    } finally { await releaseStart(); }
  }).catch(()=>{dialog.showErrorBox('工作台未能启动','请检查程序组件和本地目录权限，保留已有数据后重试。');app.exit(2);});
  app.on('window-all-closed',()=>{if(!closing)void (async()=>{await backend?.shutdown();})().finally(()=>app.quit());});
}
}
void start().catch(() => app.exit(2));
