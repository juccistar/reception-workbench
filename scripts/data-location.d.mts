export interface BootstrapConfig {
  version: 1;
  dataRoot?: string;
  previousRoot?: string;
  lastBackup?: {path: string; completedAt: number};
  lastResult?: {action: string; status: string; message: string; completedAt: number};
}
export interface BootstrapPaths {configFile: string; stateDir: string; defaultRoot: string}
export interface DataLocation {currentRoot: string; defaultRoot: string; configuredRoot: string | null; configFile: string; source: 'argument' | 'configured' | 'default' | 'legacy'}
export function getBootstrapPaths(env?: NodeJS.ProcessEnv): BootstrapPaths;
export function readBootstrap(configFile: string): BootstrapConfig;
export function writeBootstrap(configFile: string, next: BootstrapConfig, expectedCurrent?: BootstrapConfig): void;
export function resolveDataLocation(argv: string[], env?: NodeJS.ProcessEnv, fallbackRoot?: string): DataLocation;
export function restartArguments(argv: string[], root: string): string[];
