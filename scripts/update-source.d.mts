export type UpdateSource =
  | { kind: 'disabled'; url: '' }
  | { kind: 'generic'; url: string }
  | { kind: 'github'; url: string; owner: string; repo: string };
export function parseUpdateSource(value: unknown): UpdateSource;
export function updateProvider(source: UpdateSource):
  | { provider: 'generic'; url: string }
  | { provider: 'github'; owner: string; repo: string; private: false };
export function updateSourceFromConfig(config: unknown): string;
export function isUpdateRequestAllowed(source: UpdateSource, address: string): boolean;
