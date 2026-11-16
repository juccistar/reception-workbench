import { randomUUID } from 'node:crypto';
import { isExitDialogResponse, type ExitDialogChoice, type ExitDialogEvent } from '../shared/exit-dialog';

/** The caller supplies only the current frame, trusted event delivery and IPC source validation. */
export function createExitDialogHost<Window, Frame, Source>(options: {
  currentFrame: (window: Window) => Frame | null;
  isSource: (source: Source, window: Window, frame: Frame) => boolean;
  send: (window: Window, event: ExitDialogEvent) => void;
  timeoutMs?: number;
}) {
  type Pending = { window: Window; frame: Frame; id: string; finish: (choice: ExitDialogChoice) => void };
  let pending: Pending | undefined;

  function request(window: Window, reason?: 'save_failed'): Promise<ExitDialogChoice> {
    // Multiple close attempts cannot authorize one another.
    pending?.finish('cancel');
    let frame: Frame | null;
    try { frame = options.currentFrame(window); } catch { return Promise.resolve('cancel'); }
    if (!frame) return Promise.resolve('cancel');
    const id = randomUUID();
    return new Promise(resolve => {
      let settled = false;
      const timer = setTimeout(() => finish('cancel'), options.timeoutMs ?? 120_000);
      const finish = (choice: ExitDialogChoice) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (pending?.id === id) pending = undefined;
        try { options.send(window, { kind: 'dismiss', request_id: id }); } catch { /* Window may have closed. */ }
        resolve(choice);
      };
      pending = { window, frame, id, finish };
      try { options.send(window, { kind: 'open', request_id: id, ...(reason ? { reason } : {}) }); } catch { finish('cancel'); }
    });
  }

  function respond(source: Source, value: unknown): boolean {
    const active = pending;
    if (!active || !isExitDialogResponse(value) || value.request_id !== active.id) return false;
    try {
      if (options.currentFrame(active.window) !== active.frame || !options.isSource(source, active.window, active.frame)) return false;
    } catch { return false; }
    active.finish(value.choice);
    return true;
  }

  return { request, respond, cancel: () => pending?.finish('cancel') };
}
