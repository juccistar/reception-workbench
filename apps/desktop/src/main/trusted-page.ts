import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Windows file URLs may canonicalize the drive letter differently from argv. */
export function isWorkbenchPage(value: string, rendererFile: string): boolean {
  try {
    const url = new URL(value);
    if (url.protocol !== 'file:') return false;
    const actual = path.resolve(fileURLToPath(url));
    const expected = path.resolve(rendererFile);
    return process.platform === 'win32' ? actual.toLowerCase() === expected.toLowerCase() : actual === expected;
  } catch { return false; }
}
