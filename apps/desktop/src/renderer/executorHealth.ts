import type { Snapshot } from '../shared/api';

// Older snapshots have no health projection. Do not infer a timeout from elapsed wall time.
export function executorHealth(snapshot: Snapshot | null | undefined) {
  const health = snapshot?.executor_health;
  if (!health) return { ready: true, message: '' };
  if (health.state === 'recovering' && health.worker_alive) return { ready: false, message: '后台正在恢复，暂不能启动新任务或授权自动发送' };
  if (!health.worker_alive || ['failed', 'stalled', 'stopped'].includes(health.state)) return { ready: false, message: '后台执行异常，自动发送已暂停' };
  return { ready: true, message: '' };
}
