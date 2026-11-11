import type { AccioLaunchInfo } from '../shared/launch';

/** PowerShell literal plus Windows argument quoting; no shell/env expansion. */
export function powershellLiteral(value: string): string {
  return "'" + value.replace(/'/g, "''") + "'";
}
export function windowsArgument(value: string): string {
  return '"' + value.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/g, '$1$1') + '"';
}
export function detachedPowershellCommand(executable: string, args: string[]): string {
  return 'Start-Process -FilePath ' + powershellLiteral(executable) +
    ' -ArgumentList ' + powershellLiteral(args.map(windowsArgument).join(' '));
}

export interface AccioLaunchPlan {
  version: string;
  distribution: AccioLaunchInfo['distribution'];
  executable: string;
  developmentEntry?: string;
}

export function buildAccioLaunchInfo(plan: AccioLaunchPlan): AccioLaunchInfo {
  if (!plan.executable || [plan.executable, plan.developmentEntry].some(value => value && /[\0\r\n]/.test(value))) throw new Error('启动位置无效。');
  if (plan.distribution === 'development' && !plan.developmentEntry) throw new Error('开发启动入口不可用。');
  const args: string[] = [];
  if (plan.distribution === 'development') args.push(plan.developmentEntry!);
  args.push('--connect-from-accio');
  const command = detachedPowershellCommand(plan.executable, args);
  const instructions = '请仅在允许启动此工作台的当前目标商家 Accio Work 授权 PowerShell 会话中执行以下命令。\n\n'
    + command + '\n\n'
    + '该命令启动本机当前版本后立即返回，请勿等待桌面进程退出。';
  return { version: plan.version, distribution: plan.distribution, executable_path: plan.executable, command, instructions };
}
