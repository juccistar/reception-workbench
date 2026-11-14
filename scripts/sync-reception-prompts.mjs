import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Authoring command only: runtime imports the generated Python constants and
// never reads repository docs. Keep reviewed wording and executable prompts equal.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = await readFile(path.join(root, 'docs/接待提示词审核稿.md'), 'utf8');
const section = marker => {
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`Missing reviewed prompt: ${marker}`);
  const end = source.indexOf('\n## ', start + marker.length);
  return source.slice(start, end < 0 ? undefined : end);
};
const block = marker => {
  const match = /```text\r?\n([\s\S]*?)\r?\n```/.exec(section(marker));
  if (!match) throw new Error(`Missing prompt body: ${marker}`);
  return match[1].replaceAll('\r\n', '\n').trim() + '\n';
};
const prompts = {
  COMMON: block('## 3. P01：'),
  AUTOMATIC_TASK: block('## 4. P02：'),
  WHATSAPP_TASK: block('## 3A. WHATSAPP：'),
  REQUIREMENT_CHANGE_RULES: block('## 4A. P02-A：'),
  MANUAL_TASK: block('## 5. P03：'),
  POLISH_TASK: block('## 6. P04：'),
  REQUIREMENTS_TASK: block('## 11A. REQUIREMENTS：'),
  IMAGE_NATIVE: block('### P05-A：'),
  IMAGE_COMPAT: block('### P05-B：'),
  TRANSLATION: block('## 8. P06：'),
  LEGACY_TRANSLATION: block('## 9. P07：'),
  LEGACY_OBSERVATION: block('## 10. P08：'),
};
const output = ['"""Reviewed, shared generation prompts. Regenerate with scripts/sync-reception-prompts.mjs."""',
  'PROMPT_VERSION = "reception-0.3.19"',
  ...Object.entries(prompts).map(([name, value]) => `${name} = ${JSON.stringify(value)}`),
  'AUTOMATIC = COMMON + "\\n" + AUTOMATIC_TASK + "\\n" + REQUIREMENT_CHANGE_RULES + "\\ncontext =\\n"',
  'WHATSAPP = COMMON + "\\n" + WHATSAPP_TASK + "\\ncontext =\\n"',
  'MANUAL = COMMON + "\\n" + MANUAL_TASK + "\\n" + REQUIREMENT_CHANGE_RULES + "\\ncontext =\\n"',
  'POLISH = COMMON + "\\n" + POLISH_TASK + "\\ncontext =\\n"',
  'REQUIREMENTS = COMMON + "\\n" + REQUIREMENTS_TASK + "\\n" + REQUIREMENT_CHANGE_RULES + "\\ncontext =\\n"',
  '',
].join('\n');
const target = path.join(root, 'services/reception/src/reception_service/integrations/reception_prompts.py');
const check = process.argv.includes('--check');
if (check) {
  if (await readFile(target, 'utf8') !== output) throw new Error('Reviewed prompts and generated constants differ; regenerate before building.');
} else {
  await writeFile(target, output, 'utf8');
}
console.log(JSON.stringify({ status: check ? 'matched' : 'generated', prompts: Object.keys(prompts), version: 'reception-0.3.19' }));
