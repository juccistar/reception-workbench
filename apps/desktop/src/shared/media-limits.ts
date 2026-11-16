// Shared limits for original images and conversation supplement uploads.
export const MAX_ORIGINAL_BYTES = 20 * 1024 * 1024;
export const MAX_IMAGE_DIMENSION = 32768;
export const MAX_IMAGE_PIXELS = 40_000_000;
export const MAX_COPY_PNG_BYTES = 128 * 1024 * 1024;
export const IMAGE_TRANSFER_TIMEOUT_MS = 60_000;
export const SUPPLEMENT_MAX_FILE_BYTES = 20 * 1024 * 1024;
export const SUPPLEMENT_MAX_TOTAL_BYTES = 64 * 1024 * 1024;
export const SUPPLEMENT_MAX_FILES = 10;
export const SUPPLEMENT_MAX_ENCODED_BYTES = Math.ceil(SUPPLEMENT_MAX_FILE_BYTES / 3) * 4;
export const SUPPLEMENT_UPLOAD_ENVELOPE_BYTES = SUPPLEMENT_MAX_ENCODED_BYTES + 64 * 1024;
