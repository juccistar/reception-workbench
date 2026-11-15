import assert from 'node:assert/strict';
import { extractFile } from '@electron/asar';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { inflateSync } from 'node:zlib';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';

const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i], value = process.argv[i + 1];
  assert(['--label', '--executable'].includes(key) && value && !options.has(key), 'Use --label and --executable');
  options.set(key, value);
}
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(options.get('--label') || ''));
assert(options.has('--executable'));
const executable = assertReleasePath(root, options.get('--executable'));
const packagedVersion = JSON.parse(extractFile(path.join(path.dirname(path.dirname(executable)), 'app.asar'), 'package.json').toString('utf8')).version;
assert.equal(packagedVersion, '0.4.0');
const directory = path.join(root, 'artifacts/verification', `packaged-models-040-${options.get('--label')}`);
await mkdir(directory);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-packaged-models-040-'));
const report = { status: 'failed', executable: path.relative(root, executable), frozen_backend: true, provider: 'synthetic_loopback_http', real_model_calls: 0, merchant_database_opened: false, checks: [] };
let current, serverError;
function imageColor(encoded) {
  const bytes = Buffer.from(encoded, 'base64');
  assert(bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])));
  const chunks = [];
  for (let offset = 8; offset + 12 <= bytes.length;) {
    const size = bytes.readUInt32BE(offset), kind = bytes.toString('ascii', offset + 4, offset + 8);
    assert(offset + 12 + size <= bytes.length);
    if (kind === 'IHDR') { assert.equal(bytes.readUInt32BE(offset + 8), 48); assert.equal(bytes.readUInt32BE(offset + 12), 48); }
    if (kind === 'IDAT') chunks.push(bytes.subarray(offset + 8, offset + 8 + size));
    offset += 12 + size;
  }
  const pixels = inflateSync(Buffer.concat(chunks), { maxOutputLength: 10000 });
  assert.equal(pixels[0], 0);
  return pixels[1] > pixels[3] ? 'red' : 'blue';
}
const server = createServer(async (request, response) => {
  try {
    assert(current);
    const parts = []; let size = 0;
    for await (const chunk of request) { size += chunk.length; assert(size <= 1024 * 1024); parts.push(chunk); }
    const body = JSON.parse(Buffer.concat(parts).toString('utf8'));
    const anthropic = current.protocol === 'anthropic';
    assert.equal(request.url, anthropic ? '/v1/messages' : '/v1/chat/completions');
    assert.equal(anthropic ? request.headers['x-api-key'] : request.headers.authorization,
      anthropic ? 'synthetic-api-key-only' : 'Bearer synthetic-api-key-only');
    const content = body.messages.find(message => message.role === 'user').content;
    const prompt = content.filter(block => block.type === 'text').map(block => block.text).join('\n');
    const images = content.filter(block => block.type === (anthropic ? 'image' : 'image_url'));
    const observations = images.map((image, index) => ({ alias: `p${index + 1}`, color: imageColor(anthropic ? image.source.data : image.image_url.url.split(',')[1]) }));
    current.requests++; current.image_attachments += images.length;
    let value;
    if (prompt.includes('reception JSON schema')) value = { action: 'ask', reply_messages: [{ body: 'Could you share more details?', reply_sources: ['m1'] }], requirement_changes: [], conversation_summary: '客户询问图片。', observations };
    else if (images.length) value = { observations };
    else if (prompt.includes('2 + 3')) value = { answer: 5 };
    else if (prompt.includes('Translate')) value = { items: [{ id: 't1', text: '你好' }] };
    else value = { items: [{ id: 'x', count: 2 }] };
    const text = JSON.stringify(value);
    const result = anthropic
      ? { id: 'msg_synthetic', type: 'message', role: 'assistant', model: 'synthetic-model', content: [{ type: 'text', text }], stop_reason: 'end_turn', stop_sequence: null, usage: { input_tokens: 1, output_tokens: 1 } }
      : { id: 'chatcmpl_synthetic', object: 'chat.completion', created: 1, model: 'synthetic-model', choices: [{ index: 0, message: { role: 'assistant', content: text }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } };
    response.writeHead(200, { 'content-type': 'application/json' }); response.end(JSON.stringify(result));
  } catch (error) { serverError = error; response.writeHead(500); response.end('{}'); }
});
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}/v1`;
  for (const protocol of ['chat-completions', 'anthropic']) for (const purpose of ['reception', 'translation']) {
    current = { protocol, purpose, requests: 0, image_attachments: 0 };
    const packet = { format: 'model-check-v1', purpose, profile: { profile_id: 'synthetic-profile', protocol, base_url: base, model: 'synthetic-model', config_revision: 1, credential_revision: 1 }, api_key: 'synthetic-api-key-only' };
    const env = {};
    for (const [key, value] of Object.entries(process.env)) if (['systemroot', 'windir', 'systemdrive', 'path', 'pathext', 'comspec', 'userprofile'].includes(key.toLowerCase())) env[key] = value;
    Object.assign(env, { TEMP: temporary, TMP: temporary, LOCALAPPDATA: path.join(temporary, 'local'), PYTHONIOENCODING: 'utf-8' });
    const child = spawn(executable, ['--model-check'], { cwd: root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', timedOut = false;
    child.stdout.on('data', bytes => { stdout += bytes; if (stdout.length > 1024 * 1024) child.kill(); });
    child.stderr.on('data', bytes => { if (stderr.length < 16384) stderr += bytes; });
    const timer = setTimeout(() => { timedOut = true; child.kill(); }, 100000);
    child.stdin.end(JSON.stringify(packet) + '\n');
    const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); }).finally(() => clearTimeout(timer));
    assert(!timedOut, 'Frozen model checker timed out'); assert.equal(code, 0, stderr);
    const result = JSON.parse(stdout.trim());
    assert.equal(result.status, 'passed', JSON.stringify(result)); assert.equal(result.purpose, purpose);
    if (serverError) throw serverError;
    assert.equal(current.requests, purpose === 'reception' ? 7 : 2);
    assert.equal(current.image_attachments, purpose === 'reception' ? 7 : 0);
    report.checks.push({ ...current, result });
  }
  report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
finally {
  await new Promise(resolve => server.close(resolve));
  await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  await writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify({ status: report.status, checks: report.checks.length, report: path.relative(root, path.join(directory, 'report.json')) }));
