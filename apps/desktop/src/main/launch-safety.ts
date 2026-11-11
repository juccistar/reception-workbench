import path from 'node:path';
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { restartArguments, readBootstrap } from '../../../../scripts/data-location.mjs';

const same = (a: string, b: string) => process.platform === 'win32' ? path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase() : path.resolve(a) === path.resolve(b);
const keys = (value: any, expected: string[]) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === expected.sort().join(',');

// The launch context is only a routing hint. Independently check persisted registration.
export function registeredNewRoot(registryDirectory: string | undefined, stateDirectory: string, root: string, seller: string): boolean {
  try {
    if (!registryDirectory || !path.isAbsolute(registryDirectory) || !/^[1-9][0-9]{0,127}$/.test(seller)) return false;
    const state = path.join(registryDirectory, 'profiles', createHash('sha256').update(seller).digest('hex'), 'state');
    const file = path.join(registryDirectory, 'merchants.json');
    for (const item of [registryDirectory, state, root, file]) {
      const stat = lstatSync(item);
      if (stat.isSymbolicLink() || !same(realpathSync.native(item), item) || (item === file ? !stat.isFile() || stat.size > 1024 * 1024 : !stat.isDirectory())) return false;
    }
    const bytes = readFileSync(file);
    if (bytes.length > 1024 * 1024) return false;
    const registry = JSON.parse(bytes.toString('utf8'));
    if (!keys(registry, ['format', 'profiles']) || registry.format !== 1 || !registry.profiles || typeof registry.profiles !== 'object' || Array.isArray(registry.profiles) || Object.keys(registry.profiles).length > 512) return false;
    const record = registry.profiles[seller];
    if (!keys(record, ['sellerId', 'stateDirectory', 'initialDataRoot', 'status']) || record.sellerId !== seller || record.status !== 'created' || typeof record.stateDirectory !== 'string' || typeof record.initialDataRoot !== 'string' || !path.isAbsolute(record.stateDirectory) || !path.isAbsolute(record.initialDataRoot)) return false;
    const saved = readBootstrap(path.join(state, 'storage.json'));
    return same(stateDirectory, state) && same(record.stateDirectory, state) && same(record.initialDataRoot, root) && !!saved.dataRoot && same(saved.dataRoot, root) && (!saved.previousRoot || same(saved.previousRoot, root));
  } catch { return false; }
}

export function workbenchRestartArguments(args: string[], root: string): string[] {
  if (!args.includes('--connect-from-accio')) return restartArguments(args, root);
  const result: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (['--data-dir', '--workctl'].includes(args[i])) { i++; continue; }
    if (/^--(?:data-dir|workctl)=/.test(args[i])) continue;
    result.push(args[i]);
  }
  return result;
}

const messages: Record<string, string> = {
  authorized_environment_missing: '当前授权环境不完整，请在已授权的 Accio 会话中重新启动。',
  runtime_component_missing: '授权运行环境组件不可用，请检查程序组件。',
  release_missing: '工作台程序缺失，请检查程序组件。', release_invalid: '工作台程序入口无效，请检查程序组件。',
  launch_components_missing: '启动组件不完整，请安装完整的工作台程序。',
  workctl_invalid: '无法定位当前账号的 workctl，请检查授权环境。',
  seller_invalid: '指定的商家身份无效。', arguments_invalid: '启动参数无效。',
  seller_mismatch: '当前授权商家与原窗口商家不一致，未打开工作区。',
  identity_probe_invalid: '无法识别当前授权商家，未打开工作区。', identity_probe_failed: '无法核验当前授权商家，未打开工作区。',
  identity_probe_timeout: '核验当前授权商家超时，未打开工作区。', identity_probe_output_limit: '商家身份核验响应无效，未打开工作区。',
  merchant_workspace_missing: '已登记的商家工作区缺失，未创建空工作区。',
  merchant_workspace_path_too_long: '商家工作区路径过长，请选择较短的数据目录；已有数据请人工迁移。',
  merchant_initialization_incomplete: '上次商家工作区初始化未完成，请保留原目录并检查恢复信息。',
  merchant_recovery_timeout: '商家工作区恢复超时，请保留原目录和恢复备份，核查恢复进程及登记状态后再试。',
  merchant_profile_unavailable: '商家启动配置不可用，请保留配置以便恢复。',
  merchant_profile_root_conflict: '指定目录与商家已保存的工作区不一致。',
  merchant_workspace_mismatch: '工作区属于其他商家，未修改数据。',
  merchant_legacy_ownership_unknown: '无法确认原工作区归属，请保留目录并核查。',
  merchant_workspace_ownership_unknown: '无法确认工作区归属，未修改数据。',
  merchant_ownership_unavailable: '无法检查工作区归属，未修改数据。',
  merchant_path_invalid: '工作区路径不可用，请检查本地目录。', merchant_path_conflict: '商家数据目录与启动目录冲突。',
  merchant_registry_invalid: '商家登记信息无效，请保留配置以便恢复。', merchant_registry_unavailable: '商家登记目录不可用。',
  merchant_registration_conflict: '商家登记不完整或存在冲突，请保留配置并核查。', registry_lock_unavailable: '商家配置正在更新，请稍后重试。',
};
export const genericLaunchMessage = '无法识别当前授权环境或工作区。请在已授权的 Accio 会话中重新执行启动指令，并检查程序组件。';
export function launchErrorMessage(bytes: Buffer, overflow: boolean): string {
  if (overflow || bytes.length > 16384) return genericLaunchMessage;
  try {
    const parsed = JSON.parse(bytes.toString('utf8').trim());
    if (!parsed || parsed.status !== 'blocked' || typeof parsed.code !== 'string' || !Object.hasOwn(messages, parsed.code)) return genericLaunchMessage;
    const diagnostic = parsed.diagnostic;
    if (typeof diagnostic === 'string' && path.isAbsolute(diagnostic) && !/[\x00-\x1f]/.test(diagnostic) && path.basename(path.dirname(diagnostic)) === 'diagnostics' && /^[0-9a-f-]{36}\.json$/i.test(path.basename(diagnostic))) return `${messages[parsed.code]}\n启动诊断：${diagnostic}`;
    return messages[parsed.code];
  } catch { return genericLaunchMessage; }
}
