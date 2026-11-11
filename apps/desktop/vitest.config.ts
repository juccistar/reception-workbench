import { defineConfig } from 'vitest/config';
export default defineConfig({ test: {
  environment: 'jsdom', globals: true,
  include: ['apps/desktop/tests/**/*.test.{ts,tsx}'],
  setupFiles: ['apps/desktop/tests/setup.ts'],
  restoreMocks: true, testTimeout: 10000,
  pool: 'forks', maxWorkers: 1,
} });
