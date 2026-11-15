// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { observeWhatsAppInitialize } from '../../../scripts/fixtures/whatsapp-initialize-hook';

it('forwards the SDK and all initialize arguments while observing real injection', async () => {
  const inject = vi.fn(async () => {});
  const client = { inject };
  const puppeteer = { connect: vi.fn() };
  const extra = {};
  const initialize = vi.fn(async (received: typeof client, _generation: number, sdk: typeof puppeteer, _extra: object) => {
    await sdk.connect();
    await received.inject();
    return 'initialized';
  });
  const host = { initialize };
  const observed = vi.fn();
  observeWhatsAppInitialize(host, observed);
  await expect(host.initialize(client, 7, puppeteer, extra)).resolves.toBe('initialized');
  expect(initialize).toHaveBeenCalledExactlyOnceWith(client, 7, puppeteer, extra);
  expect(puppeteer.connect).toHaveBeenCalledOnce();
  expect(inject).toHaveBeenCalledOnce();
  expect(observed).toHaveBeenCalledOnce();
});

it('injects the controlled failure only after SDK injection completes', async () => {
  const inject = vi.fn(async () => {});
  const client = { inject };
  const host = { initialize: async (value: typeof client) => value.inject() };
  observeWhatsAppInitialize(host, () => { throw new Error('controlled failure'); });
  await expect(host.initialize(client)).rejects.toThrow('controlled failure');
  expect(inject).toHaveBeenCalledOnce();
});
