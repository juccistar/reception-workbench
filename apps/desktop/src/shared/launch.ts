export interface AccioLaunchInfo {
  version: string;
  distribution: 'installed' | 'portable' | 'development';
  executable_path: string;
  command: string;
  instructions: string;
}
