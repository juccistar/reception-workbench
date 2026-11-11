export type UpdateStatus = 'unavailable' | 'idle' | 'checking' | 'available' | 'downloading' | 'downloaded' | 'installing' | 'error';
export interface UpdateSettings { feed_url: string }
export interface UpdateInfo {
  current_version: string;
  available_version: string | null;
  status: UpdateStatus;
  installed: boolean;
  feed_url: string;
  checked_at: number | null;
  progress: { percent: number; transferred: number; total: number } | null;
  release_notes: string;
  message: string;
}
