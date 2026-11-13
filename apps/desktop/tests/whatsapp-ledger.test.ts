// @vitest-environment node
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { SendLedger } from '../src/main/whatsapp/ledger';

const dirs: string[] = [];
afterEach(() => { for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });
describe('WhatsApp submission ledger', () => {
  it('persists the intent before submission and never reopens an unknown operation', () => {
    const dir = mkdtempSync(join(tmpdir(), 'wa-ledger-')); dirs.push(dir);
    const intent = { operation_id: 'op', account_id: 'account', connection_epoch: 'epoch', chat_id: 'opaque@lid', body: 'frozen' };
    const ledger = new SendLedger(dir);
    expect(ledger.begin(intent)).toBe(true);
    const restarted = new SendLedger(dir);
    expect(restarted.read('op')).toEqual({ intent, result: undefined });
    expect(restarted.begin({ ...intent, body: 'different' })).toBe(false);
    restarted.finish('op', { message_id: 'exact-id', ack: 0 });
    expect(new SendLedger(dir).read('op')).toEqual({ intent, result: { message_id: 'exact-id', ack: 0 } });
    expect(restarted.begin(intent)).toBe(false);
  });
});
