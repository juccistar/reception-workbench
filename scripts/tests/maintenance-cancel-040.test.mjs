import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runRecoveryProcess } from '../connect-live.mjs';

test('recovery timeout requests cancellation and waits for child close while draining stderr', async () => {
 const program = `process.stdin.setEncoding('utf8'); process.stderr.write('x'.repeat(1000000)); process.stdin.on('data', () => setTimeout(() => { process.stdout.write(JSON.stringify({status:'completed'}), () => process.exit(0)); }, 150));`;
 const started = Date.now();
 const result = await runRecoveryProcess({executable:process.execPath,prefix:['-e',program],cwd:process.cwd(),env:process.env}, [], 20);
 assert.equal(result.status, 'completed');
 assert.ok(Date.now()-started >= 140);
});

test('abnormal recovery exit is rejected only after close', async () => {
 await assert.rejects(runRecoveryProcess({executable:process.execPath,prefix:['-e','process.exit(2)'],cwd:process.cwd(),env:process.env}, [], 1000), /merchant_initialization_incomplete/);
});
