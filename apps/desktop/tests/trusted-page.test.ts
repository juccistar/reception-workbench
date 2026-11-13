// @vitest-environment node
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { expect, it } from 'vitest';
import { isWorkbenchPage } from '../src/main/trusted-page';

it('accepts the same local renderer despite Windows drive-letter normalization', () => {
  const renderer = path.resolve('artifacts/app/renderer/index.html');
  const url = pathToFileURL(renderer).href;
  expect(isWorkbenchPage(url, renderer)).toBe(true);
  if (process.platform === 'win32') {
    expect(isWorkbenchPage(url.replace(/file:\/\/\/([A-Z]):/, (_, letter: string) => `file:///${letter.toLowerCase()}:`), renderer.toUpperCase())).toBe(true);
  }
  expect(isWorkbenchPage(pathToFileURL(path.resolve('artifacts/app/renderer/other.html')).href, renderer)).toBe(false);
  expect(isWorkbenchPage('https://example.com/index.html', renderer)).toBe(false);
  expect(isWorkbenchPage('not a URL', renderer)).toBe(false);
});
