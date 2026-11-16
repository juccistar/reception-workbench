import { createHash } from 'node:crypto';
import { lstat, readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

const FILES = ['background.js', 'manifest.json', 'panel.css', 'panel.html', 'panel.js'];
/** The native Chrome extension is a small, fixed production resource, outside ASAR. */
export async function inspectCustomerPanel(directory, version) {
  if (!path.isAbsolute(directory) || path.resolve(directory) !== directory) throw new Error('Invalid customer panel directory.');
  let current = path.parse(directory).root;
  for (const part of directory.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    const entry = await lstat(current);
    if (!entry.isDirectory() || entry.isSymbolicLink()) throw new Error('Customer panel links are not allowed.');
  }
  if (JSON.stringify((await readdir(directory)).sort()) !== JSON.stringify(FILES)) throw new Error('Unexpected customer panel files.');
  const files = [];
  let manifest;
  for (const name of FILES) {
    const file = path.join(directory, name), entry = await lstat(file);
    if (!entry.isFile() || entry.isSymbolicLink() || entry.nlink !== 1 || entry.size > 256 * 1024) throw new Error('Invalid customer panel file.');
    const bytes = await readFile(file);
    if (bytes.length !== entry.size) throw new Error('Customer panel changed during packaging.');
    files.push({ path: name, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') });
    if (name === 'manifest.json') manifest = JSON.parse(bytes.toString('utf8'));
  }
  if (manifest.manifest_version !== 3 || manifest.version !== version || JSON.stringify(manifest.permissions) !== '["sidePanel"]' || manifest.side_panel?.default_path !== 'panel.html' || manifest.background?.service_worker !== 'background.js' || manifest.host_permissions || manifest.content_scripts || manifest.externally_connectable || manifest.web_accessible_resources || !manifest.content_security_policy?.extension_pages?.includes("connect-src 'none'")) throw new Error('Unexpected customer panel capabilities.');
  return { format: 'customer-panel-files-v1', version, files };
}
