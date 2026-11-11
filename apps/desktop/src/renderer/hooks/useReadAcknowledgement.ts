import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import type { ConversationDetail, Message } from '../../shared/api';

// Match the backend's (timestamp, Unicode code point message ID) ordering.
export function compareMessages(a: Message, b: Message): number {
  const time = (a.timestamp_ms ?? -1) - (b.timestamp_ms ?? -1);
  if (time) return time;
  const left = Array.from(a.message_id), right = Array.from(b.message_id);
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    const diff = left[i].codePointAt(0)! - right[i].codePointAt(0)!;
    if (diff) return diff;
  }
  return left.length - right.length;
}

export type ObservedMessage = Message & { observed_sequence?: number | null };

type Options = {
  viewport: RefObject<HTMLDivElement | null>;
  conversationId: string;
  detail: ConversationDetail | null;
  messages: ObservedMessage[];
  active: boolean;
  acknowledge: (conversationId: string, throughMessageId: string, sequence: number) => Promise<void>;
};

export function useReadAcknowledgement(options: Options) {
  const latest = useRef(options);
  latest.current = options;
  const mounted = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(new Set<string>());
  const completed = useRef(new Map<string, string>());
  const failed = useRef(new Map<string, string>());
  const [failures, setFailures] = useState<Record<string, boolean>>({});
  const scheduleRef = useRef<() => void>(() => {});

  const scan = useCallback(() => {
    const current = latest.current;
    const { conversationId, detail, viewport, active, acknowledge } = current;
    const node = viewport.current;
    if (!mounted.current || !active || !node || node.dataset.conversationId !== conversationId
        || detail?.conversation.conversation_id !== conversationId || !detail.reading?.initialized
        || detail.conversation.unread_count <= 0 || document.visibilityState !== 'visible' || !document.hasFocus()
        || inFlight.current.has(conversationId)) return;
    const bounds = node.getBoundingClientRect();
    if (bounds.height <= 0 || bounds.width <= 0) return;
    let top = Math.max(0, bounds.top), bottom = Math.min(window.innerHeight, bounds.bottom);
    let left = Math.max(0, bounds.left), right = Math.min(window.innerWidth, bounds.right);
    // The conversation can scroll while an assistance form is expanded. Its
    // message viewport may then be outside the window or clipped by a parent.
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent), rect = parent.getBoundingClientRect();
      if (/auto|scroll|hidden|clip/.test(style.overflowY || style.overflow)) { top = Math.max(top, rect.top); bottom = Math.min(bottom, rect.bottom); }
      if (/auto|scroll|hidden|clip/.test(style.overflowX || style.overflow)) { left = Math.max(left, rect.left); right = Math.min(right, rect.right); }
    }
    if (bottom <= top || right <= left) return;
    const known = new Set(current.messages.map(message => message.message_id));
    let boundary: string | undefined;
    for (const item of node.querySelectorAll<HTMLElement>('[data-message-id]')) {
      const rect = item.getBoundingClientRect();
      // The bottom of the message must have been visible. Scrolling an older
      // section acknowledges only that section's boundary, never the unseen tail.
      if (known.has(item.dataset.messageId ?? '') && rect.height > 0 && rect.width > 0
          && rect.bottom > top && rect.bottom <= bottom + 1
          && rect.right > left && rect.left < right) {
        const probeX = Math.max(left, rect.left) + Math.min(12, (Math.min(right, rect.right) - Math.max(left, rect.left)) / 2);
        const painted = typeof document.elementFromPoint === 'function' ? document.elementFromPoint(probeX, Math.min(bottom - 1, rect.bottom - 1)) : null;
        if (typeof document.elementFromPoint !== 'function' || painted && item.contains(painted)) boundary = item.dataset.messageId;
      }
    }
    if (!boundary) return;
    const displayed = current.messages.find(message => message.message_id === boundary)!;
    if (displayed.observed_sequence === null) return;
    // A cached older page keeps the snapshot that actually supplied its body.
    const sequence = Math.min(detail.reading.observed_sequence, displayed.observed_sequence ?? detail.reading.observed_sequence);
    const key = JSON.stringify([sequence, boundary]);
    if (completed.current.get(conversationId) === key || failed.current.get(conversationId) === key) return;
    inFlight.current.add(conversationId);
    void acknowledge(conversationId, boundary, sequence).then(() => {
      completed.current.set(conversationId, key);
      failed.current.delete(conversationId);
      if (mounted.current) setFailures(previous => ({ ...previous, [conversationId]: false }));
    }).catch(() => {
      failed.current.set(conversationId, key);
      if (mounted.current) setFailures(previous => ({ ...previous, [conversationId]: true }));
    }).finally(() => {
      inFlight.current.delete(conversationId);
      // An arrival or customer switch while this call was pending may expose a
      // new boundary. Re-evaluate the current DOM without reusing the old owner.
      if (mounted.current) scheduleRef.current();
    });
  }, []);

  const schedule = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => { timer.current = null; scan(); }, 250);
  }, [scan]);
  scheduleRef.current = schedule;
  const retry = useCallback(() => {
    failed.current.delete(latest.current.conversationId);
    schedule();
  }, [schedule]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; if (timer.current) clearTimeout(timer.current); };
  }, []);
  const renderedMessages = JSON.stringify(options.messages.map(message => [message.message_id, message.observed_sequence]));
  useEffect(() => {
    const node = options.viewport.current;
    if (!options.active || !node) return;
    schedule();
    node.addEventListener('scroll', schedule, { passive: true });
    document.addEventListener('scroll', schedule, { capture: true, passive: true });
    window.addEventListener('focus', schedule);
    document.addEventListener('visibilitychange', schedule);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    observer?.observe(node);
    return () => {
      node.removeEventListener('scroll', schedule);
      document.removeEventListener('scroll', schedule, true);
      window.removeEventListener('focus', schedule);
      document.removeEventListener('visibilitychange', schedule);
      observer?.disconnect();
      if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    };
  }, [options.active, options.conversationId, options.detail?.reading?.observed_sequence,
    options.detail?.conversation.unread_count, renderedMessages, options.viewport, schedule]);

  return { failed: !!failures[options.conversationId], retry };
}
