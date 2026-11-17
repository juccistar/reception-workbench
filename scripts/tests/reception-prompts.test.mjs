import test from 'node:test';
import assert from 'node:assert/strict';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../../', import.meta.url));
const auditText = readFileSync(path.join(root, 'docs/接待提示词审核稿.md'), 'utf8').replaceAll('\r\n', '\n');

function fixture() {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'reception-prompt-generation-'));
  for (const relative of ['docs', 'scripts', 'services/reception/src/reception_service/integrations']) {
    mkdirSync(path.join(directory, relative), {recursive: true});
  }
  const script = path.join(directory, 'scripts/sync-reception-prompts.mjs');
  const document = path.join(directory, 'docs/接待提示词审核稿.md');
  const generated = path.join(directory, 'services/reception/src/reception_service/integrations/reception_prompts.py');
  copyFileSync(path.join(root, 'scripts/sync-reception-prompts.mjs'), script);
  writeFileSync(document, auditText, 'utf8');
  return {directory, script, document, generated};
}

function run(current, ...args) {
  return spawnSync(process.execPath, [current.script, ...args], {cwd: root, encoding: 'utf8'});
}

function generate(current) {
  const result = run(current);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

function appendToBlock(document, marker, addition) {
  const start = document.indexOf(marker);
  assert.notEqual(start, -1);
  const body = document.indexOf('```text\n', start);
  assert.notEqual(body, -1);
  const end = document.indexOf('\n```', body + '```text\n'.length);
  assert.notEqual(end, -1);
  return document.slice(0, end) + '\n' + addition + document.slice(end);
}

test('generator checks all reviewed blocks and preserves translation cache revision when reception wording changes', () => {
  const current = fixture();
  try {
    const before = generate(current);
    assert.equal(before.prompts.length, 12);
    assert.equal(new Set(before.prompts).size, 12);
    assert.equal(run(current, '--check').status, 0);
    writeFileSync(current.document, auditText.replaceAll('\n', '\r\n'), 'utf8');
    assert.equal(run(current, '--check').status, 0);
    assert.equal(generate(current).translation_version, before.translation_version);
    writeFileSync(current.document, appendToBlock(auditText, '## 3. P01：', '生成器隔离测试：仅接待规则变化。'), 'utf8');
    assert.notEqual(run(current, '--check').status, 0);
    const after = generate(current);
    assert.equal(after.translation_version, before.translation_version);
    assert.equal(run(current, '--check').status, 0);
  } finally {
    rmSync(current.directory, {recursive: true, force: true});
  }
});

test('editing the translation prompt changes its cache revision in the generated module', () => {
  const current = fixture();
  try {
    const before = generate(current);
    writeFileSync(current.document, appendToBlock(auditText, '## 8. P06：', '生成器隔离测试：翻译正文变化。'), 'utf8');
    const after = generate(current);
    assert.notEqual(after.translation_version, before.translation_version);
    assert.equal(after.version, before.version);
    const module = readFileSync(current.generated, 'utf8');
    assert.ok(module.includes('TRANSLATION_PROMPT_VERSION = ' + JSON.stringify(after.translation_version)));
    assert.equal(run(current, '--check').status, 0);
  } finally {
    rmSync(current.directory, {recursive: true, force: true});
  }
});
