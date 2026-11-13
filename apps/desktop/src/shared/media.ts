export type ImageReference =
  | { kind: 'message'; conversation_id: string; message_id: string; source_version: string; sha256: string }
  | { kind: 'requirement'; case_id: string; media_id: string; source_version: string; sha256: string }
  | { kind: 'product'; local_product_id: string; sha256: string; version: number }
  | { kind: 'supplement'; conversation_id: string; attachment_id: string; sha256: string };

export interface OriginalImage { bytes: Uint8Array; media_type: string; width: number; height: number; sha256: string }
