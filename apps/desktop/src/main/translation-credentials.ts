import { randomBytes } from 'node:crypto';
import { constants } from 'node:fs';
import { link, lstat, mkdir, open, rename, unlink } from 'node:fs/promises';
import path from 'node:path';

export interface CredentialEncryption {
  isEncryptionAvailable(): boolean;
  encryptString(value: string): Buffer | Promise<Buffer>;
  decryptString(value: Buffer): string | Promise<string>;
}

const REF = /^[a-f0-9]{64}$/;
const MAX_SECRET_BYTES = 8192;
const MAX_CIPHERTEXT_BYTES = 1024 * 1024;

function validSecret(value: unknown, maxBytes = MAX_SECRET_BYTES): value is string {
  return typeof value === 'string' && value.trim().length > 0 &&
    Buffer.byteLength(value, 'utf8') <= maxBytes && !/[\0\r\n]/.test(value);
}

function missing(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'ENOENT';
}

export class TranslationCredentialStore {
  private readonly directory: string;

  constructor(directory: string, private readonly encryption: CredentialEncryption, private readonly maxSecretBytes = MAX_SECRET_BYTES) {
    if (!Number.isSafeInteger(maxSecretBytes) || maxSecretBytes < 1 || maxSecretBytes > MAX_CIPHERTEXT_BYTES) throw new Error('Invalid translation credential size');
    this.directory = path.resolve(directory);
  }

  // Check each existing component so a junction or symbolic link cannot redirect the store.
  private async checkDirectory(create: boolean): Promise<boolean> {
    const parsed = path.parse(this.directory);
    let current = parsed.root;
    for (const part of this.directory.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
      current = path.join(current, part);
      try {
        const stat = await lstat(current);
        if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error();
      } catch (error) {
        if (!missing(error)) throw error;
        if (!create) return false;
        try { await mkdir(current, { mode: 0o700 }); } catch (mkdirError) {
          if (typeof mkdirError !== 'object' || mkdirError === null || !('code' in mkdirError) || mkdirError.code !== 'EEXIST') throw mkdirError;
        }
        const stat = await lstat(current);
        if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error();
      }
    }
    return true;
  }

  async put(secret: string): Promise<string> {
    if (!validSecret(secret, this.maxSecretBytes)) throw new Error('Invalid translation credential');
    let temporary: string | undefined;
    try {
      if (!this.encryption.isEncryptionAvailable()) throw new Error();
      const ciphertext = await this.encryption.encryptString(secret);
      if (!Buffer.isBuffer(ciphertext) || ciphertext.length === 0 || ciphertext.length > MAX_CIPHERTEXT_BYTES) throw new Error();
      await this.checkDirectory(true);
      const ref = randomBytes(32).toString('hex');
      temporary = path.join(this.directory, `.${randomBytes(32).toString('hex')}.tmp`);
      const file = await open(temporary, 'wx', 0o600);
      try {
        await file.writeFile(ciphertext);
        await file.sync();
      } finally {
        await file.close();
      }
      // A hard link publishes the complete file atomically and fails if the ref exists.
      await link(temporary, path.join(this.directory, ref));
      return ref;
    } catch {
      throw new Error('Unable to store translation credential');
    } finally {
      if (temporary) await unlink(temporary).catch(() => {});
    }
  }

  /** Publish a complete encrypted replacement at a stable reference. Callers serialize writers. */
  async replace(ref: string, secret: string): Promise<void> {
    if (typeof ref !== 'string' || !REF.test(ref)) throw new Error('Invalid translation credential reference');
    if (!validSecret(secret, this.maxSecretBytes)) throw new Error('Invalid translation credential');
    let temporary: string | undefined;
    try {
      if (!this.encryption.isEncryptionAvailable()) throw new Error();
      const ciphertext = await this.encryption.encryptString(secret);
      if (!Buffer.isBuffer(ciphertext) || ciphertext.length === 0 || ciphertext.length > MAX_CIPHERTEXT_BYTES) throw new Error();
      await this.checkDirectory(true);
      const target = path.join(this.directory, ref);
      temporary = path.join(this.directory, `.${randomBytes(32).toString('hex')}.tmp`);
      const file = await open(temporary, 'wx', 0o600);
      try {
        await file.writeFile(ciphertext);
        await file.sync();
      } finally { await file.close(); }
      let stat;
      try { stat = await lstat(target); } catch (error) { if (!missing(error)) throw error; }
      if (stat && (stat.isSymbolicLink() || !stat.isFile() || stat.nlink !== 1)) throw new Error();
      await rename(temporary, target);
      temporary = undefined;
    } catch {
      throw new Error('Unable to store translation credential');
    } finally {
      if (temporary) await unlink(temporary).catch(() => {});
    }
  }

  async get(ref: string | null | undefined): Promise<string | null> {
    if (ref == null) return null;
    if (typeof ref !== 'string' || !REF.test(ref)) throw new Error('Invalid translation credential reference');
    try {
      if (!await this.checkDirectory(false)) return null;
      const filename = path.join(this.directory, ref);
      for (let attempt = 0; attempt < 3; attempt++) {
        let stat;
        try { stat = await lstat(filename); } catch (error) { if (missing(error)) return null; throw error; }
        if (stat.isSymbolicLink() || !stat.isFile() || stat.nlink !== 1 || stat.size > MAX_CIPHERTEXT_BYTES) throw new Error();
        const file = await open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
        let ciphertext: Buffer | undefined;
        try {
          const opened = await file.stat();
          if (!opened.isFile() || opened.size > MAX_CIPHERTEXT_BYTES) throw new Error();
          // A replacement may land between lstat and open; retry against its new inode.
          if (opened.dev === stat.dev && opened.ino === stat.ino && opened.nlink === 1) ciphertext = await file.readFile();
        } finally { await file.close(); }
        if (!ciphertext) continue;
        if (!this.encryption.isEncryptionAvailable()) throw new Error();
        const secret = await this.encryption.decryptString(ciphertext);
        if (!validSecret(secret, this.maxSecretBytes)) throw new Error();
        return secret;
      }
      throw new Error();
    } catch {
      throw new Error('Unable to read translation credential');
    }
  }

  async remove(ref: string): Promise<void> {
    if (typeof ref !== 'string' || !REF.test(ref)) throw new Error('Invalid translation credential reference');
    try {
      if (!await this.checkDirectory(false)) return;
      const filename = path.join(this.directory, ref);
      let stat;
      try { stat = await lstat(filename); } catch (error) { if (missing(error)) return; throw error; }
      if (stat.isSymbolicLink() || !stat.isFile() || stat.nlink !== 1) throw new Error();
      try { await unlink(filename); } catch (error) { if (!missing(error)) throw error; }
    } catch {
      throw new Error('Unable to remove translation credential');
    }
  }
}
