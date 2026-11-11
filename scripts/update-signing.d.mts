export const UPDATE_APP_ID: 'local.inquiry.reception.workbench';
export interface ReleaseFields { version: string; artifact: string; sha512: string; size: number }
export interface ReleaseSignature { format: 1; app_id: typeof UPDATE_APP_ID; signature: string }
export function signReleaseMetadata(fields: ReleaseFields, privateKey: string): ReleaseSignature;
export function verifyReleaseMetadata(metadata: unknown, publicKey: string): ReleaseFields | null;
export function loadOrCreateReleaseKeys(directory?: string): Promise<{ privateKey: string; publicKey: string; directory: string }>;
