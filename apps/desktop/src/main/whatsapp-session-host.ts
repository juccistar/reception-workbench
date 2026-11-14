import { createHash, randomUUID } from 'node:crypto';
import { open, readFile, rename, unlink } from 'node:fs/promises';
import { join } from 'node:path';

interface SessionRecord { session_generation: number; recovery_id: string; recovery_stage: 'committed' }
type SessionFile = { version: 1; merchants: Record<string, SessionRecord> };

export class WhatsAppSessionHost {
  private readonly file: string;
  private writes: Promise<unknown> = Promise.resolve();
  constructor(private readonly profileRoot: string) { this.file = join(profileRoot, 'whatsapp-session-generation.json'); }
  private merchant(seller: string): string {
    if (!/^[1-9]\d*$/.test(seller)) throw new Error('商家身份无效。');
    return createHash('sha256').update(seller).digest('hex');
  }
  private async read(): Promise<SessionFile> {
    let text: string;
    try { text = await readFile(this.file, 'utf8'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { version: 1, merchants: {} }; throw error; }
    try {
      const data = JSON.parse(text) as SessionFile;
      if (!data || data.version !== 1 || !data.merchants || typeof data.merchants !== 'object' || Array.isArray(data.merchants)) throw new Error();
      for (const [key, record] of Object.entries(data.merchants)) {
        if (!/^[0-9a-f]{64}$/.test(key) || !record || !Number.isSafeInteger(record.session_generation) || record.session_generation < 1 || typeof record.recovery_id !== 'string' || !/^wa_recovery_[0-9a-f-]{36}$/.test(record.recovery_id) || record.recovery_stage !== 'committed') throw new Error();
      }
      return data;
    } catch { throw new Error('WhatsApp 登录环境配置损坏，请保留原目录并检查配置。'); }
  }
  async generation(seller: string): Promise<number> { return (await this.read()).merchants[this.merchant(seller)]?.session_generation ?? 0; }
  partition(seller: string, generation: number): string {
    this.merchant(seller);
    if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('WhatsApp 登录环境代次无效。');
    const source = this.profileRoot + ':' + seller + (generation ? ':generation:' + generation : '');
    return 'persist:whatsapp-' + createHash('sha256').update(source).digest('hex');
  }
  commit(seller: string, expected: number, recoveryId: string): Promise<number> {
    const result = this.writes.then(() => this.commitGeneration(seller, expected, recoveryId));
    this.writes = result.catch(() => {});
    return result;
  }
  private async commitGeneration(seller: string, expected: number, recoveryId: string): Promise<number> {
    if (!/^wa_recovery_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(recoveryId)) throw new Error('WhatsApp 恢复编号无效。');
    const data = await this.read(), key = this.merchant(seller), previous = data.merchants[key];
    if (previous?.recovery_id === recoveryId) return previous.session_generation;
    if ((previous?.session_generation ?? 0) !== expected || expected >= Number.MAX_SAFE_INTEGER) throw new Error('WhatsApp 登录环境已变化，请重新确认。');
    const generation = expected + 1;
    data.merchants[key] = { session_generation: generation, recovery_id: recoveryId, recovery_stage: 'committed' };
    const temporary = this.file + '.' + randomUUID() + '.tmp';
    try {
      const handle = await open(temporary, 'wx', 0o600);
      try { await handle.writeFile(JSON.stringify(data), 'utf8'); await handle.sync(); }
      finally { await handle.close(); }
      await rename(temporary, this.file);
    } finally { await unlink(temporary).catch(() => {}); }
    return generation;
  }
}
