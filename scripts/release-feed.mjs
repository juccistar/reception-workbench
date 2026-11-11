import { createHash } from 'node:crypto';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { assertReleasePath } from './release-paths.mjs';
import { signReleaseMetadata } from './update-signing.mjs';
import { parseUpdateSource, updateProvider } from './update-source.mjs';

export function parsePackageArgs(args, root) {
  let output, updateUrl, installer = false, candidate = false;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === '--installer') installer = true;
    else if (arg === '--candidate') candidate = true;
    else if (arg === '--output' || arg === '--update-url') {
      if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error(`${arg} requires a value`);
      if (arg === '--output') { if (output !== undefined) throw new Error('Duplicate --output'); output = args[++i]; }
      else { if (updateUrl !== undefined) throw new Error('Duplicate --update-url'); updateUrl = args[++i]; }
    } else throw new Error(`Unknown argument: ${arg}`);
  }
  if (updateUrl !== undefined) {
    const source = parseUpdateSource(updateUrl);
    if (source.kind === 'disabled') throw new Error('--update-url requires a public GitHub repository or HTTPS directory');
    updateUrl = source.url;
    if (!installer) throw new Error('--update-url requires --installer');
  }
  const version = output === undefined ? JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')).version : '';
  if (output === undefined && !/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Invalid package version');
  const resolved = assertReleasePath(root, output ?? path.join(root, `relase/release-${version}`));
  return { output: resolved, installer, candidate, updateUrl };
}

export function updateConfig(updateUrl, publisherName, cacheName = 'inquiry-reception-workbench-updater') {
  const config = { updaterCacheDirName: cacheName };
  if (updateUrl) Object.assign(config, updateProvider(parseUpdateSource(updateUrl)));
  if (publisherName && (Array.isArray(publisherName) ? publisherName.length : true)) config.publisherName = publisherName;
  return JSON.stringify(config, null, 2) + '\n'; // JSON is valid YAML.
}

export async function createReleaseFeed({ output, installer, version, signed, privateKey }) {
  const name = path.basename(installer);
  if (!name.endsWith('.exe') || !version) throw new Error('Invalid installer or version');
  const bytes = await readFile(installer);
  const sha512 = createHash('sha512').update(bytes).digest('base64');
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const receptionSignature = signReleaseMetadata({ version, artifact: name, sha512, size: bytes.length }, privateKey);
  const feed = path.join(output, 'release-feed');
  await mkdir(feed, { recursive: false });
  await copyFile(installer, path.join(feed, name));
  const blockmap = installer + '.blockmap';
  const blockmapBytes = await readFile(blockmap);
  await copyFile(blockmap, path.join(feed, path.basename(blockmap)));
  const metadata = { version, files: [{ url: name, sha512, size: bytes.length }], path: name, sha512, releaseDate: new Date().toISOString(), releaseNotes: `更新至 ${version}。请查看随包使用与更新说明。`, reception_signature: receptionSignature };
  await writeFile(path.join(feed, 'latest.yml'), JSON.stringify(metadata, null, 2) + '\n');
  const files = [name, path.basename(blockmap), 'latest.yml'];
  const entries = [];
  for (const file of files) {
    const data = await readFile(path.join(feed, file));
    entries.push({ file, size: data.length, sha256: createHash('sha256').update(data).digest('hex'), sha512: createHash('sha512').update(data).digest('base64') });
  }
  const manifest = { format: 1, version, signed: signed === true, release_signature: 'ed25519', installer: name, files: entries };
  await writeFile(path.join(output, 'release-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  await writeFile(path.join(installer + '.sha256'), `${sha256}  ${name}\n`);
  await writeFile(path.join(output, '发布说明.txt'), '将 release-feed 中安装包和 blockmap 上传至 HTTPS 更新目录，验证下载完整后最后上传 latest.yml。首次使用需手动安装此版本。更新源地址可在应用设置中填写。\n');
  return { feed, manifest, sha512, size: bytes.length, blockmapSize: blockmapBytes.length };
}
