import { createHash } from 'node:crypto';
import { copyFile, lstat, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { root } from './paths.mjs';

const requiredRoots = { 'whatsapp-web.js': '1.34.8', puppeteer: '24.38.0' };
const excludedDirs = new Set(['node_modules', 'test', 'tests', '__tests__', 'example', 'examples', 'docs', 'doc', 'coverage', '.git', '.cache', 'cache', '.local-chromium', 'chrome', 'chromium', 'firefox', 'user_data', 'session']);
const excludedNames = /^(?:install|postinstall|preinstall)\.(?:js|cjs|mjs)$/i;
const runtimeFile = name => /\.(?:js|cjs|mjs|json|wasm)$/i.test(name) || /^(?:licen[sc]e|copying|notice)(?:\..*)?$/i.test(name);
const digest = data => createHash('sha256').update(data).digest('hex');
const validName = name => /^(?:@[a-z0-9._~-]+\/)?[a-z0-9._~-]+$/i.test(name) && !name.includes('..');

function resolveLock(packages, from, name) {
  if (!validName(name)) throw new Error('Invalid package name in lockfile');
  let dir = from;
  while (true) {
    const candidate = (dir ? dir + '/' : '') + 'node_modules/' + name;
    if (packages[candidate]) return candidate;
    if (!dir) return undefined;
    dir = path.posix.dirname(dir);
    if (dir === '.') dir = '';
  }
}

async function lockedClosure() {
  const packages = JSON.parse(await readFile(path.join(root, 'package-lock.json'), 'utf8')).packages;
  const queue = [];
  for (const [name, version] of Object.entries(requiredRoots)) {
    const key = resolveLock(packages, '', name);
    if (!key || packages[key].version !== version || packages[''].dependencies?.[name] !== version) throw new Error('Unexpected WhatsApp dependency version');
    queue.push(key);
  }
  const selected = new Set();
  while (queue.length) {
    const key = queue.pop();
    if (selected.has(key)) continue;
    selected.add(key);
    const entry = packages[key];
    for (const name of Object.keys(entry.dependencies || {})) {
      const dependency = resolveLock(packages, key, name);
      if (!dependency) throw new Error(`Missing locked dependency ${name}`);
      queue.push(dependency);
    }
    for (const name of Object.keys(entry.optionalDependencies || {})) {
      const dependency = resolveLock(packages, key, name);
      if (dependency) {
        try { await lstat(path.join(root, ...dependency.split('/'))); queue.push(dependency); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
    }
  }
  return { packages, selected };
}

/** Stage the locked production dependency closure in target/node_modules. */
export async function stageWhatsAppRuntime(target) {
  const { packages, selected } = await lockedClosure();
  const destination = path.resolve(target);
  // Reject an existing dependency tree: staging must not merge with unrelated files.
  await mkdir(destination, { recursive: true });
  await mkdir(path.join(destination, 'node_modules'));
  const files = [];
  const packageList = [];
  async function copyTree(source, relative, packageKey) {
    for (const entry of await readdir(source, { withFileTypes: true })) {
      const from = path.join(source, entry.name);
      const rel = relative ? relative + '/' + entry.name : entry.name;
      if (entry.isSymbolicLink()) throw new Error('Symlink in WhatsApp runtime package');
      if (entry.isDirectory()) {
        if (!excludedDirs.has(entry.name.toLowerCase())) await copyTree(from, rel, packageKey);
      } else if (entry.isFile() && runtimeFile(entry.name) && !(relative === '' && excludedNames.test(entry.name))) {
        const destRel = packageKey + '/' + rel;
        const dest = path.join(destination, ...destRel.split('/'));
        await mkdir(path.dirname(dest), { recursive: true });
        await copyFile(from, dest);
        const data = await readFile(dest);
        files.push({ path: destRel, sha256: digest(data), size: data.length });
      }
    }
  }
  for (const key of [...selected].sort()) {
    const source = path.join(root, ...key.split('/'));
    const info = JSON.parse(await readFile(path.join(source, 'package.json'), 'utf8'));
    if (info.version !== packages[key].version) throw new Error(`Installed package version differs from lockfile: ${key}`);
    packageList.push({ path: key, name: info.name, version: info.version });
    await copyTree(source, '', key);
  }
  files.sort((a, b) => a.path.localeCompare(b.path));
  if (!files.some(file => file.path.endsWith('/puppeteer-core/lib/cjs/puppeteer/generated/injected.js'))) throw new Error('Missing Puppeteer injected script');
  const manifest = { format: 1, packages: packageList, files };
  await writeFile(path.join(destination, 'whatsapp-runtime-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  return manifest;
}

/** Reject missing, altered or unlisted files in a staged dependency tree. */
export async function verifyWhatsAppRuntime(target) {
  const base = path.resolve(target);
  const manifest = JSON.parse(await readFile(path.join(base, 'whatsapp-runtime-manifest.json'), 'utf8'));
  if (manifest.format !== 1 || !Array.isArray(manifest.files) || !Array.isArray(manifest.packages)) throw new Error('Invalid WhatsApp runtime manifest');
  const { packages, selected } = await lockedClosure();
  const recorded = new Set();
  for (const item of manifest.packages) {
    if (!selected.has(item.path) || recorded.has(item.path) || packages[item.path].version !== item.version || !validName(item.name)) throw new Error('Invalid WhatsApp runtime package');
    recorded.add(item.path);
  }
  if (recorded.size !== selected.size) throw new Error('Missing WhatsApp runtime package');
  const expected = new Map();
  for (const file of manifest.files) {
    if (typeof file.path !== 'string' || !file.path.startsWith('node_modules/') || file.path.includes('\\') || file.path.split('/').some(part => !part || part === '.' || part === '..') || expected.has(file.path) || !/^[a-f0-9]{64}$/.test(file.sha256) || !Number.isSafeInteger(file.size)) throw new Error('Invalid WhatsApp runtime manifest path');
    const owner = [...selected].filter(key => file.path.startsWith(key + '/')).sort((a, b) => b.length - a.length)[0];
    if (!owner) throw new Error('File outside locked dependency closure');
    const parts = file.path.slice(owner.length + 1).split('/');
    if (parts.slice(0, -1).some(part => excludedDirs.has(part.toLowerCase())) || !runtimeFile(parts.at(-1)) || (parts.length === 1 && excludedNames.test(parts[0]))) throw new Error('File outside WhatsApp runtime allowlist');
    expected.set(file.path, file);
  }
  if (![...expected.keys()].some(file => file.endsWith('/puppeteer-core/lib/cjs/puppeteer/generated/injected.js'))) throw new Error('Missing Puppeteer injected script');
  async function check(dir, rel) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const next = rel + '/' + entry.name;
      const full = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) throw new Error('Symlink in staged WhatsApp runtime');
      if (entry.isDirectory()) await check(full, next);
      else if (entry.isFile()) {
        const item = expected.get(next);
        if (!item) throw new Error(`Unlisted WhatsApp runtime file: ${next}`);
        const data = await readFile(full);
        if (data.length !== item.size || digest(data) !== item.sha256) throw new Error(`Changed WhatsApp runtime file: ${next}`);
        expected.delete(next);
      } else throw new Error('Invalid staged WhatsApp runtime entry');
    }
  }
  await check(path.join(base, 'node_modules'), 'node_modules');
  if (expected.size) throw new Error('Missing WhatsApp runtime file');
  return manifest;
}
