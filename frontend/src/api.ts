export type Geo = { type: string; coordinates: any };
export type Feature = { type: 'Feature'; geometry: Geo; properties: Record<string, any> };
export type FC = { type: 'FeatureCollection'; features: Feature[]; attribution?: string };

export type Options = {
  spacing_m: number;
  sides: 'both' | 'left' | 'right';
  angles: number;
  pitch: number;
  fov: number;
  corners: boolean;
  divided_roads_near_side: boolean;
  batch_size: number;
};

export const DEFAULT_OPTIONS: Options = { spacing_m: 15, sides: 'both', angles: 1, pitch: -8, fov: 80, corners: false, divided_roads_near_side: true, batch_size: 20 };

export type Status = {
  version: string;
  jobs_folder: string;
  browser: string;
  basemap: { url: string; attribution: string };
  scope: Geo;
  local_streets: boolean;
};

export type PlannedShot = { code: string; lat: number; lng: number; heading: number; fov: number; side: string; kind: string; street: string; look: string; url: string };

export type Preview = {
  summary: { shots: number; points: number; streets: number; length_m: number; batches: number; intersections: number; divided_road_views_skipped: number };
  warnings: string[];
  shots: PlannedShot[];
  shots_omitted: boolean;
};

export type Counts = Record<string, number> & { total: number; done: number };

export type JobSummary = {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  checked_at: string | null;
  options: Options;
  batch_size: number;
  counts: Counts;
  folder: string;
  batches: number;
  storage: { saved_bytes: number; estimated_total_bytes: number | null };
};

export type Shot = PlannedShot & {
  id: string;
  ordinal: number;
  batch: number;
  view_kind: string;
  pitch: number;
  filename: string;
  footpath: string;
  near_node_m: number | null;
  captured_at: string | null;
  capture_source: string | null;
  status: string;
  flags: string[];
  note: string;
  result: Record<string, any>;
  image_url: string | null;
  thumb_url: string | null;
};

export type Job = JobSummary & { area: Geo | null; streets: FC | null; street_count: number; bounds: [number, number, number, number] | null; warnings?: string[]; unknown_log_entries?: string[]; changed?: number };

export type ShotPage = { total: number; items: Shot[] };

export type Prompt = { text: string; shots: string[]; label: string; log_file: string; batches: number };

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data?.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((x: any) => x.msg ?? String(x)).join('; ') : `Request failed (${res.status})`);
  }
  return data as T;
}

export const api = {
  get: <T,>(p: string) => request<T>('GET', p),
  post: <T,>(p: string, b?: unknown) => request<T>('POST', p, b),
  patch: <T,>(p: string, b?: unknown) => request<T>('PATCH', p, b),
  del: <T,>(p: string) => request<T>('DELETE', p),
};
