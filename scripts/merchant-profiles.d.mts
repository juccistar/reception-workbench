export interface MerchantProfile { sellerId: string; dataRoot: string; stateDirectory: string }
export interface OwnershipResult { status: 'missing' | 'empty' | 'owned' | 'unknown' | 'multiple'; sellerId?: string; reason?: string }
export function workspacePathUnits(dataRoot: string): number;
export function maintenanceEnvironment(env?: NodeJS.ProcessEnv): NodeJS.ProcessEnv;
export interface MerchantProfileOptions {
  dataRoot?: string;
  programDirectory?: string;
  inspectOwnership?: (root: string) => Promise<OwnershipResult>;
  backend?: { executable: string; prefix: string[]; env?: NodeJS.ProcessEnv; cwd?: string };
}
export const AUTO_LAUNCH_CONTEXT_ENV: string;
export function merchantRegistryDirectory(env: NodeJS.ProcessEnv): string;
export function merchantSupportPaths(registryDirectory: string): { dataDirectory: string; backupDirectory: string };
export function resolveMerchantProfile(sellerId: string, env: NodeJS.ProcessEnv, options?: MerchantProfileOptions): Promise<MerchantProfile>;
export function consumeAutoLaunchContext(env: NodeJS.ProcessEnv, args: string[]): MerchantProfile | null;
