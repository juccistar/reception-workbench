// @vitest-environment node
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { beforeEach, expect, it, vi } from 'vitest';
import { ServiceClient } from '../src/main/service-client';

const seam = vi.hoisted(() => ({ child: undefined as any }));
vi.mock('node:child_process', () => ({ spawn: () => seam.child }));
beforeEach(() => {
  seam.child = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough() });
});

it('rejects unavailable RPCs on error but keeps shutdown pending until the owned child closes', async () => {
  const client = new ServiceClient('synthetic', [], process.cwd(), {});
  const pending = client.invoke('get_whatsapp_state').catch(error => error);
  seam.child.emit('error', new Error('synthetic process failure'));
  expect(await pending).toMatchObject({ code: 'backend_unavailable' });
  expect(client.hasExited()).toBe(true);
  let finished = false;
  const shutdown = client.shutdown().then(() => { finished = true; });
  await Promise.resolve();
  expect(finished).toBe(false);
  seam.child.emit('close', 1);
  await shutdown;
  expect(finished).toBe(true);
});

it('drops stdout events drained after a child disconnects', () => {
  const client = new ServiceClient('synthetic', [], process.cwd(), {});
  const forwarded: unknown[] = [];
  client.on('event', event => forwarded.push(event));
  seam.child.stdout.write(JSON.stringify({ event: 'job.updated', data: { job_id: 'before' } }) + '\n');
  seam.child.emit('exit', 1);
  seam.child.stdout.write(JSON.stringify({ event: 'job.updated', data: { job_id: 'late' } }) + '\n');
  expect(forwarded).toEqual([
    { event: 'job.updated', data: { job_id: 'before' } },
    expect.objectContaining({ event: 'backend.disconnected' }),
  ]);
  seam.child.emit('close', 1);
});

it('waits for close after a normal exit rather than treating its event as complete cleanup', async () => {
  const client = new ServiceClient('synthetic', [], process.cwd(), {});
  seam.child.stdin.on('data', (data: Buffer) => {
    const request = JSON.parse(data.toString());
    seam.child.stdout.write(JSON.stringify({ id: request.id, result: {} }) + '\n');
  });
  let finished = false;
  const shutdown = client.shutdown().then(() => { finished = true; });
  await Promise.resolve();
  seam.child.emit('exit', 0);
  await Promise.resolve();
  expect(finished).toBe(false);
  seam.child.emit('close', 0);
  await shutdown;
  expect(finished).toBe(true);
});
