import { createHash } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export interface FrozenIntent {
  operation_id: string;
  account_id: string;
  connection_epoch: string;
  chat_id: string;
  body: string;
}
export interface SendResult { message_id: string; ack: number }
export interface LedgerRecord { intent: FrozenIntent; result?: SendResult }

/** One append-only file per operation. A durable intent is also the submission lock. */
export class SendLedger {
  constructor(private readonly directory: string) {
    mkdirSync(directory, { recursive: true });
  }
  private path(operation: string): string {
    return join(this.directory, createHash('sha256').update(operation).digest('hex') + '.jsonl');
  }
  read(operation: string): LedgerRecord | undefined {
    const path = this.path(operation);
    if (!existsSync(path)) return undefined;
    const lines = readFileSync(path, 'utf8').split('\n');
    // An incomplete final line means the result was not durably recorded.
    const intent = JSON.parse(lines[0]) as FrozenIntent;
    if (intent.operation_id !== operation) throw new Error('Invalid send ledger');
    let result: SendResult | undefined;
    if (lines.length > 2 && lines[1]) result = JSON.parse(lines[1]) as SendResult;
    return { intent, result };
  }
  begin(intent: FrozenIntent): boolean {
    let fd: number;
    try { fd = openSync(this.path(intent.operation_id), 'wx', 0o600); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') return false;
      throw error;
    }
    try { writeFileSync(fd, JSON.stringify(intent) + '\n'); fsyncSync(fd); }
    finally { closeSync(fd); }
    // On platforms which support directory sync, persist the new directory entry too.
    try { const dir = openSync(this.directory, 'r'); try { fsyncSync(dir); } finally { closeSync(dir); } } catch { /* Windows does not open directories this way. */ }
    return true;
  }
  finish(operation: string, result: SendResult): void {
    const fd = openSync(this.path(operation), 'a');
    try { writeFileSync(fd, JSON.stringify(result) + '\n'); fsyncSync(fd); }
    finally { closeSync(fd); }
  }
}
