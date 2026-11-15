// Coalesce each key independently, including changes that arrive during a read.
export function createEventRefresh(run: (key: string) => Promise<void>, delay = 70) {
  const entries = new Map<string, { timer?: ReturnType<typeof setTimeout>; running: boolean; dirty: boolean }>();
  let stopped = false;
  const drain = async (key: string, entry: { timer?: ReturnType<typeof setTimeout>; running: boolean; dirty: boolean }) => {
    entry.timer = undefined;
    if (stopped || entry.running) return;
    entry.running = true;
    try {
      while (entry.dirty && !stopped) {
        entry.dirty = false;
        try { await run(key); } catch { /* individual reads report their own errors */ }
      }
    } finally {
      entry.running = false;
      if (entries.get(key) === entry) entries.delete(key);
    }
  };
  return {
    request(key = '') {
      if (stopped) return;
      let entry = entries.get(key);
      if (!entry) { entry = { running: false, dirty: false }; entries.set(key, entry); }
      entry.dirty = true;
      if (!entry.running && entry.timer === undefined) {
        const current = entry;
        entry.timer = setTimeout(() => void drain(key, current), delay);
      }
    },
    stop() {
      stopped = true;
      for (const entry of entries.values()) if (entry.timer !== undefined) clearTimeout(entry.timer);
      entries.clear();
    },
  };
}
