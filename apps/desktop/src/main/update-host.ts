import { readFileSync } from 'node:fs';
import { mkdir, writeFile, rename, rm } from 'node:fs/promises';
import path from 'node:path';
import type { UpdateInfo } from '../shared/updates';
import { verifyReleaseMetadata } from '../../../../scripts/update-signing.mjs';
import { parseUpdateSource, updateProvider, isUpdateRequestAllowed } from '../../../../scripts/update-source.mjs';

export interface UpdateEngine {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  autoRunAppAfterInstall: boolean;
  allowDowngrade: boolean;
  allowPrerelease: boolean;
  disableWebInstaller: boolean;
  logger: unknown;
  readonly installerPath?: string | null;
  on(event: string, listener: (...args: any[]) => void): unknown;
  removeListener(event: string, listener: (...args: any[]) => void): unknown;
  checkForUpdates(): Promise<unknown>;
  downloadUpdate(): Promise<unknown>;
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void;
}

export interface UpdateHostOptions {
  currentVersion: string;
  installed: boolean;
  settingsFile: string;
  onChange?: (info: UpdateInfo) => void;
  trustedPublicKey?: string;
  defaultFeedUrl?: string;
  beforeInstall?: (version: string, installerPath: string, sha512: string) => Promise<boolean>;
  completeInstall?: () => Promise<void> | void;
  onInstallError?: () => Promise<void>;
  createEngine?: (url: string) => Promise<UpdateEngine> | UpdateEngine;
}

function cleanUrl(value: unknown): string { return parseUpdateSource(value).url; }

function notesText(input: unknown): string {
  const parts = Array.isArray(input) ? input.map(item => typeof item === 'object' && item ? (item as { note?: unknown }).note : '').join('\n') : input;
  if (typeof parts !== 'string') return '';
  return parts.replace(/<[^>]*>/g, ' ').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '').slice(0, 4000);
}

async function defaultEngine(url: string): Promise<UpdateEngine> {
  const { NsisUpdater } = await import('electron-updater');
  const source = parseUpdateSource(url);
  const updater = new NsisUpdater(updateProvider(source));
  updater.netSession.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: !isUpdateRequestAllowed(source, details.url) });
  });
  return updater as unknown as UpdateEngine;
}

class UpdateVerificationError extends Error {}

export class UpdateHost {
  private info: UpdateInfo;
  private generation = 0;
  private engine: UpdateEngine | null = null;
  private verifiedSha512: string | null = null;
  private active: { kind: string; promise: Promise<UpdateInfo> } | null = null;
  private saving = false;

  constructor(private readonly options: UpdateHostOptions) {
    let feed = '';
    try {
      const stored = JSON.parse(readFileSync(options.settingsFile, 'utf8')) as { feed_url?: unknown };
      feed = cleanUrl(stored.feed_url);
    } catch (error) {
      // Only an absent settings file uses the build default; explicit empty settings disable updates.
      if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') {
        try { feed = cleanUrl(options.defaultFeedUrl ?? ''); } catch { /* Invalid defaults fail closed. */ }
      }
    }
    this.info = { current_version: options.currentVersion, available_version: null, status: options.installed ? 'idle' : 'unavailable', installed: options.installed, feed_url: feed, checked_at: null, progress: null, release_notes: '', message: options.installed ? '' : '当前程序不是安装版，无法在线更新。' };
  }

  getInfo(): UpdateInfo { return { ...this.info, progress: this.info.progress ? { ...this.info.progress } : null }; }
  private change(patch: Partial<UpdateInfo>): void {
    this.info = { ...this.info, ...patch };
    try { this.options.onChange?.(this.getInfo()); } catch { /* Observers cannot interrupt an update. */ }
  }
  private fail(message: string): UpdateInfo { this.change({ status: this.options.installed ? 'error' : 'unavailable', message, progress: null }); return this.getInfo(); }
  private run(kind: string, fn: () => Promise<UpdateInfo>): Promise<UpdateInfo> {
    if (this.active) return this.active.kind === kind ? this.active.promise : Promise.resolve(this.getInfo());
    if (this.saving) return Promise.resolve(this.getInfo());
    const promise = Promise.resolve().then(fn).finally(() => { if (this.active?.promise === promise) this.active = null; });
    this.active = { kind, promise };
    return promise;
  }

  async saveSettings(input: unknown): Promise<UpdateInfo> {
    if (this.saving || this.active?.kind === 'install') return this.getInfo();
    let feed: string;
    try {
      if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(key => key !== 'feed_url')) throw new Error('invalid');
      feed = cleanUrl((input as { feed_url?: unknown }).feed_url);
    } catch { this.change({ message: '更新源地址无效，请填写 GitHub 仓库或 HTTPS 目录地址。' }); return this.getInfo(); }
    if (feed && feed === this.info.feed_url) return this.getInfo();
    this.saving = true;
    try {
      const file = this.options.settingsFile;
      await mkdir(path.dirname(file), { recursive: true });
      const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
      try { await writeFile(tmp, JSON.stringify({ feed_url: feed }), { encoding: 'utf8', mode: 0o600 }); await rename(tmp, file); }
      finally { await rm(tmp, { force: true }).catch(() => undefined); }
      this.generation++;
      this.engine = null; this.verifiedSha512 = null;
      this.change({ feed_url: feed, status: this.options.installed ? 'idle' : 'unavailable', available_version: null, checked_at: null, progress: null, release_notes: '', message: this.options.installed ? '' : '当前程序不是安装版，无法在线更新。' });
      return this.getInfo();
    } catch { return this.fail('保存更新设置失败，请重试。'); }
    finally { this.saving = false; }
  }

  check(): Promise<UpdateInfo> {
    return this.run('check', async () => {
      if (!this.options.installed) return this.fail('当前程序不是安装版，无法在线更新。');
      if (!this.info.feed_url) return this.fail('请先设置 HTTPS 更新源。');
      if (!this.options.trustedPublicKey) return this.fail('更新验证配置不可用，请联系管理员。');
      const generation = ++this.generation;
      const feed = this.info.feed_url;
      this.engine = null; this.verifiedSha512 = null;
      this.change({ status: 'checking', available_version: null, checked_at: null, release_notes: '', progress: null, message: '正在检查更新。' });
      let engine: UpdateEngine | null = null;
      let available: { version?: unknown; releaseNotes?: unknown } | null = null;
      let engineError = false;
      const onAvailable = (value: { version?: unknown; releaseNotes?: unknown }) => { available = value; };
      const onError = () => { engineError = true; };
      try {
        engine = await (this.options.createEngine ?? defaultEngine)(feed);
        if (generation !== this.generation) return this.getInfo();
        // Some providers emit late errors after a request settles; never expose them or crash.
        engine.on('error', () => undefined);
        engine.autoDownload = false; engine.autoInstallOnAppQuit = false; engine.autoRunAppAfterInstall = false;
        engine.allowDowngrade = false; engine.allowPrerelease = false; engine.disableWebInstaller = true; engine.logger = null;
        engine.on('update-available', onAvailable); engine.on('error', onError);
        await engine.checkForUpdates();
        if (generation !== this.generation) return this.getInfo();
        if (engineError) throw new Error('check');
        const candidate = available as { version?: unknown; releaseNotes?: unknown } | null;
        if (!candidate) {
          this.change({ status: 'idle', checked_at: Date.now(), message: '当前已是最新版本。' });
        } else {
          const verified = verifyReleaseMetadata(candidate, this.options.trustedPublicKey!);
          const numeric = /^\d+\.\d+\.\d+$/;
          if (!verified || candidate.version !== verified.version || !numeric.test(verified.version) || !numeric.test(this.options.currentVersion)) throw new UpdateVerificationError();
          const next = verified.version.split('.').map(BigInt);
          const current = this.options.currentVersion.split('.').map(BigInt);
          const firstDifference = next.findIndex((part, i) => part !== current[i]);
          if (firstDifference < 0 || next[firstDifference] <= current[firstDifference]) throw new UpdateVerificationError();
          this.engine = engine; this.verifiedSha512 = verified.sha512;
          this.change({ status: 'available', available_version: verified.version, checked_at: Date.now(), release_notes: notesText(candidate.releaseNotes), message: '发现新版本，可选择下载。' });
        }
      } catch (error) {
        if (generation === this.generation) this.fail(error instanceof UpdateVerificationError
          ? '更新签名、版本或安装包信息不匹配，请核对发布文件。'
          : parseUpdateSource(feed).kind === 'github'
            ? '检查 GitHub 更新失败，请确认网络可用、仓库公开且已发布含更新文件的正式版本。'
            : '检查更新失败，请检查网络和更新源后重试。');
      }
      finally { if (engine) { engine.removeListener('update-available', onAvailable); engine.removeListener('error', onError); } }
      return this.getInfo();
    });
  }

  download(): Promise<UpdateInfo> {
    return this.run('download', async () => {
      if (this.info.status !== 'available' || !this.engine || !this.info.available_version) return this.getInfo();
      const engine = this.engine; const generation = this.generation;
      let engineError = false;
      const onError = () => { engineError = true; };
      const onProgress = (value: { percent?: number; transferred?: number; total?: number }) => {
        if (generation !== this.generation || this.info.status !== 'downloading') return;
        const safe = (n: unknown) => typeof n === 'number' && Number.isFinite(n) ? Math.max(0, n) : 0;
        this.change({ progress: { percent: Math.min(100, safe(value?.percent)), transferred: safe(value?.transferred), total: safe(value?.total) } });
      };
      this.change({ status: 'downloading', progress: { percent: 0, transferred: 0, total: 0 }, message: '正在下载更新。' });
      engine.on('download-progress', onProgress); engine.on('error', onError);
      try {
        await engine.downloadUpdate();
        if (generation === this.generation) {
          if (engineError) throw new Error('download');
          this.change({ status: 'downloaded', progress: null, message: '下载完成，可以安装并重启。' });
        }
      } catch {
        if (generation === this.generation) this.change({ status: 'available', progress: null, message: '下载更新失败，请重试。' });
      } finally { engine.removeListener('download-progress', onProgress); engine.removeListener('error', onError); }
      return this.getInfo();
    });
  }

  install(): Promise<UpdateInfo> {
    return this.run('install', async () => {
      if (this.info.status !== 'downloaded' || !this.engine || !this.info.available_version || !this.options.beforeInstall) return this.getInfo();
      const engine = this.engine; const version = this.info.available_version;
      const installerPath = engine.installerPath;
      if (typeof installerPath !== 'string' || !installerPath.trim() || !this.verifiedSha512) return this.fail('安装包不可用，请重新检查并下载更新。');
      this.change({ status: 'installing', message: '正在准备安装更新。' });
      try {
        if (!await this.options.beforeInstall(version, installerPath, this.verifiedSha512)) {
          this.change({ status: 'downloaded', message: '已取消安装，可以稍后重试。' });
          return this.getInfo();
        }
        let recovery: Promise<void> | null = null;
        const recover = () => {
          if (!recovery) recovery = (async () => {
            engine.removeListener('error', recover);
            try { await this.options.onInstallError?.(); } catch { /* Preserve the safe error state. */ }
            if (this.info.status === 'installing') this.change({ status: 'downloaded', message: '安装更新失败，请重试。' });
          })();
        };
        engine.on('error', recover);
        try {
          if (this.options.completeInstall) await this.options.completeInstall();
          else engine.quitAndInstall(true, false);
        } catch { recover(); }
        if (recovery) await recovery;
      } catch {
        try { await this.options.onInstallError?.(); } catch { /* Preserve the safe error state. */ }
        this.change({ status: 'downloaded', message: '安装更新失败，请重试。' });
      }
      return this.getInfo();
    });
  }
}
