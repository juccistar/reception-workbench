import { readFileSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { isReleasePath } from './release-paths.mjs';

// Business launch follows the promoted delivery, not an unreleased package.json.
export function currentRuntime(root) {
  try {
    const artifacts = realpathSync(path.join(root, 'artifacts'));
    const insideArtifacts = file => {
      const relative = path.relative(artifacts, realpathSync(file));
      return relative !== '' && relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative);
    };
    const delivery = JSON.parse(readFileSync(path.join(artifacts, 'delivery.json'), 'utf8'));
    if (!isReleasePath(root, delivery.executable)) throw new Error();
    const release = path.resolve(root, delivery.executable);
    const releaseManifest = path.join(path.dirname(release), 'manifest.json');
    if (path.basename(release) !== 'ReceptionWorkbench.exe' || !isReleasePath(root, releaseManifest)) throw new Error();
    realpathSync(release); realpathSync(releaseManifest);
    const version = JSON.parse(readFileSync(releaseManifest, 'utf8')).version;
    if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) throw new Error();
    // The dated path is a compatibility fallback for already delivered versions.
    for (const name of [`runtime-${version}`, `runtime-${version}-20261107`]) {
      const folder = path.join(artifacts, name);
      const executable = path.join(folder, 'ReceptionWorkbench.exe');
      const manifest = path.join(folder, 'manifest.json');
      try {
        if (insideArtifacts(executable) && insideArtifacts(manifest) && JSON.parse(readFileSync(manifest, 'utf8')).version === version) return { version, executable };
      } catch {
        // An absent or invalid candidate may still have a valid dated fallback.
      }
    }
  } catch {
    // Report all unusable delivery metadata through the launcher's stable error code.
  }
  throw new Error('runtime_copy_missing');
}
