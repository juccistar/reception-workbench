import { lstatSync, realpathSync } from 'node:fs';
import path from 'node:path';

// Resolve existing ancestors without following links; permit future output paths.
export function assertReleasePath(root, value) {
  if (typeof value !== 'string' || !value || value.split(/[\\/]+/).includes('..')) throw new Error('release_path_invalid');
  const base = path.resolve(root);
  const resolved = path.resolve(base, value);
  const relative = path.relative(base, resolved);
  const parts = relative.split(path.sep);
  if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative) || !['artifacts', 'relase'].includes(parts[0]) || parts.length < 2) throw new Error('release_path_invalid');
  let current = base;
  for (const part of ['', ...parts]) {
    if (part) current = path.join(current, part);
    try {
      if (lstatSync(current).isSymbolicLink()) throw new Error('release_path_invalid');
      // realpath detects junctions and other indirections, including Windows reparse points.
      const real = realpathSync(current);
      if (path.relative(current, real) !== '') throw new Error('release_path_invalid');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  return resolved;
}
export function isReleasePath(root, value) {
  try { assertReleasePath(root, value); return true; } catch { return false; }
}
