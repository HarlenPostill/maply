import type { Shot } from './api';

export const STATUS_WORDS: Record<string, string> = {
  pending: 'Not taken yet',
  captured: 'Saved',
  needs_check: 'Needs a look',
  no_imagery: 'No imagery',
  blocked: 'Footpath blocked',
  accepted: 'Looks right',
  retake: 'Retake',
};

export function flagWords(shot: Shot): string[] {
  const r = shot.result || {};
  const map: Record<string, string> = {
    moved: `Camera ${r.camera_offset_m ?? '?'} m from the planned spot`,
    far_from_plan: `Camera ${r.camera_offset_m ?? '?'} m from the planned spot`,
    heading_off: `Facing ${Math.abs(r.heading_delta ?? 0).toFixed(0)}° away from the planned direction`,
    wrong_side: 'Facing the other footpath',
    not_street_view: 'The logged link is not a Street View link',
    url_not_lined_up: 'The logged link is the original link, not the lined-up view',
    no_final_url: 'Codex didn’t log the address-bar link',
    not_logged: 'Missing from Codex’s log',
    no_image_date: 'No capture date logged',
    file_missing: 'Logged as saved, but the file is missing',
    renamed_file: 'Saved under a different file name',
    small_image: 'Image is smaller than expected',
    unreadable_image: 'Image file can’t be opened',
  };
  return shot.flags.map((f) => map[f] ?? f);
}

export function metres(m: number): string {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

export function ago(iso: string | null): string {
  if (!iso) return 'never';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}
