import path from 'node:path';
import { resolveWindowsUserLocalAppData } from './windows-user-directories';

let pendingDirectory: Promise<string> | undefined;
export function resolveModelBudgetDirectory(): Promise<string> {
  return pendingDirectory ??= resolveWindowsUserLocalAppData().then(local => path.join(local, 'AlibabaReceptionWorkbenchHost', 'model-budget')).catch(error => { pendingDirectory = undefined; throw error; });
}
/** Overwrite inherited routing hints; all children use the same real Windows-user budget. */
export async function modelBudgetEnvironment(source: NodeJS.ProcessEnv): Promise<NodeJS.ProcessEnv> {
  const env = { ...source };
  for (const key of Object.keys(env)) if (/^RECEPTION_MODEL_BUDGET_DIRECTORY$/i.test(key)) delete env[key];
  env.RECEPTION_MODEL_BUDGET_DIRECTORY = await resolveModelBudgetDirectory();
  return env;
}
