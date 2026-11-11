import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
afterEach(() => cleanup());
if (!globalThis.ResizeObserver) globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
if (typeof HTMLElement !== 'undefined') {
  if (!HTMLElement.prototype.scrollIntoView) HTMLElement.prototype.scrollIntoView = function () {};
  if (!HTMLElement.prototype.scrollTo) HTMLElement.prototype.scrollTo = function () {};
}
