// Response / request shapes of the local maply API. Fields the backend may
// omit or null are typed as optional/nullable; UI code must tolerate both.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface Geometry {
  type: string;
  coordinates: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export interface Feature<P = Record<string, unknown>> {
  type: 'Feature';
  geometry: Geometry;
  properties: P;
}

export interface FeatureCollection<P = Record<string, unknown>> {
  type: 'FeatureCollection';
  features: Feature<P>[];
}

// ---------------------------------------------------------------- system
export interface ProcessingMode {
  allow_cloud_image_processing: boolean;
  allow_synthetic_cloud_image_processing: boolean;
  label: string;
  openai_model: string | null;
  openai_configured: boolean;
  openai_store: boolean;
}

export interface SystemCheck {
  name: string;
  status: string;
  detail: Json;
  checked_at: string | null;
}

export interface ExternalContact {
  service: string;
  from?: string;
  data?: string;
  host?: string;
  when?: string;
}

export interface SystemStatus {
  version: string;
  processing_mode: ProcessingMode;
  storage: {
    data_dir: string;
    host_data_dir?: string;
    projects_dir: string;
    exports_dir: string;
    browser_state_dir: string;
    bytes: Record<string, number> | null;
    note: string;
  };
  database: { ok: boolean };
  checks: SystemCheck[];
  workers_alive: number;
  mock_panorama_enabled: boolean;
  basemap: { url: string; attribution: string; max_zoom: number };
  operator_limits: {
    permitted_access_methods: string[];
    permitted_targets: string[];
    max_workers: number;
    max_requests_per_minute: number;
    max_captures_per_job: number;
  };
  egress_last_24h: { allowed: number; blocked: number };
  external_contacts: ExternalContact[];
}

export interface EgressRow {
  id: number;
  created_at: string | null;
  provider: string;
  endpoint: string | null;
  data_class: string | null;
  image_count: number | null;
  decision: string;
  reason: string | null;
  job_id: string | null;
}

// ---------------------------------------------------------------- projects
export interface ProjectConfig {
  purpose: string;
  approval_reference: string;
  approval_expires_on: string | null;
  permitted_access_methods: string[];
  permitted_targets: string[];
  allow_third_party_processing: boolean;
  third_party_processors: string[];
  max_captures_total: number;
  max_workers: number;
  max_requests_per_minute: number;
  retention_days: number;
  scope_buffer_m: number;
}

export interface EffectiveLimits {
  permitted_access_methods: string[];
  permitted_targets: string[];
  max_workers: number;
  max_requests_per_minute: number;
  max_captures_total: number;
  cloud_image_processing_allowed: boolean;
  synthetic_cloud_image_processing_allowed: boolean;
  retention_days: number;
  approval_expires_on: string | null;
  reasons: string[];
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  config: Partial<ProjectConfig> | null;
  scope: Geometry | null;
  created_at: string | null;
  effective_limits?: EffectiveLimits;
}

// ---------------------------------------------------------------- routes
export type RouteKind = 'route' | 'polygon' | 'import' | 'streetview_seed';

export interface RouteRow {
  id: string;
  project_id: string;
  name: string;
  kind: RouteKind | string;
  geometry: Geometry;
  properties: Record<string, unknown> | null;
  created_at: string | null;
  warnings?: string[];
}

export interface ParsedStreetView {
  lat: number | null;
  lng: number | null;
  is_street_view?: boolean;
  heading: number | null;
  pitch: number | null;
  fov: number | null;
  pano_id: string | null;
  pano_kind?: string | null;
  provenance?: string | null;
  url_form?: string | null;
  documented_url?: string | null;
  note?: string;
}

// ---------------------------------------------------------------- plans
export interface PlanParams {
  sampling_interval_m: number;
  sides: 'left' | 'right' | 'both';
  views_per_side: number;
  oblique_angle_deg: number;
  pitch_deg: number;
  fov_deg: number;
  viewport_width: number;
  viewport_height: number;
  footpath_offset_m: number;
  segment_max_length_m: number;
  turn_split_deg: number;
  include_crossing_approaches: boolean;
  include_route_endpoints_as_nodes: boolean;
  crossing_approach_offset_m: number;
  crossing_view_angle_deg: number;
  polygon_mode: 'perimeter' | 'clip_lines';
  projected_epsg?: number;
  max_imagery_age_months: number | null;
  unknown_date_policy: 'needs_review' | 'accept_flagged' | 'reject';
  max_camera_offset_m: number;
  no_imagery_distance_m: number;
  heading_tolerance_deg: number;
  reuse_panorama_views: boolean;
}

export interface PlanSummary {
  note?: string;
  nodes?: number;
  samples?: number;
  segments?: number;
  warnings?: string[];
  seed_samples?: number;
  planned_views?: number;
  projected_crs?: string;
  street_samples?: number;
  total_length_m?: number;
  crossing_approach_samples?: number;
  [k: string]: unknown;
}

export interface Plan {
  id: string;
  project_id: string;
  name: string;
  status: 'draft' | 'approved' | 'superseded' | string;
  params: Partial<PlanParams> | null;
  route_ids: string[] | null;
  summary: PlanSummary | null;
  config_version_id: string | null;
  created_at: string | null;
  approved_at: string | null;
}

export interface PlannedViewProps {
  id: string;
  side: string;
  view_kind: string;
  heading_deg: number;
  pitch_deg?: number;
  fov_deg?: number;
  basis?: string;
}

export interface SegmentProps {
  id: string;
  segment_key: string;
  ordinal: number;
  name: string;
  length_m: number;
  bearing_deg: number;
  sides: string[];
}

export interface FootpathProps {
  segment_key: string;
  side: string;
  kind: string;
}

export interface SampleProps {
  id: string;
  ordinal: number;
  kind: string;
  segment_key: string | null;
  seq_in_segment: number;
  chainage_m: number;
  local_bearing_deg: number;
  edited: boolean;
  excluded: boolean;
  note: string | null;
  node_key: string | null;
  views: PlannedViewProps[];
}

export interface NodeProps {
  id: string;
  node_key: string;
  kind: string;
  detection: string;
  segment_keys: string[];
}

export interface PlanGeoJSON {
  segments: FeatureCollection<SegmentProps>;
  footpaths: FeatureCollection<FootpathProps>;
  samples: FeatureCollection<SampleProps>;
  nodes: FeatureCollection<NodeProps>;
}

export interface PlanDetail extends Plan {
  geojson: PlanGeoJSON;
}

// ---------------------------------------------------------------- jobs
export interface CurrentTask {
  task_id: string;
  ordinal: number;
  status: string;
  segment_key: string;
  attempt: number;
  sample: { id: string; kind: string; lat: number; lng: number };
  lease_expires_at: string | null;
  reason: string | null;
  views: { id: string; side: string; view_kind: string; heading_deg: number; captured: string | null | undefined }[];
}

export interface WorkerInfo {
  id: string;
  job_id: string | null;
  slot: number;
  host: string;
  pid: number | null;
  status: string;
  method: string | null;
  message: string | null;
  heartbeat_at: string | null;
  heartbeat_age_s: number | null;
  started_at: string | null;
  latest_screenshot_url: string | null;
  latest_screenshot_at: string | null;
  browser_profile: string | null;
  current_task: CurrentTask | null;
}

export interface ModelUsage {
  calls: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  images_sent: number;
  estimated_cost_usd: number;
  label: string;
}

export interface JobSettings {
  mock?: Record<string, unknown>;
  policy?: Record<string, unknown>;
  viewport?: { width: number; height: number };
  max_attempts?: number;
  analysis_crop?: unknown;
  [k: string]: unknown;
}

export interface Job {
  id: string;
  project_id: string;
  plan_id: string;
  config_version_id: string | null;
  name: string;
  status: string;
  target: string;
  method: string;
  worker_count: number;
  requests_per_minute: number;
  max_captures: number;
  max_duration_s: number;
  model_budget_usd: number;
  runtime_s: number;
  pause_kind: string | null;
  pause_reason: string | null;
  settings: JobSettings | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  task_counts: Record<string, number> | null;
  attempts_total: number;
  captures_current: number;
  model_usage: ModelUsage | null;
  egress: { allowed: number; blocked: number } | null;
  workers?: WorkerInfo[];
}

export interface JobCreate {
  name: string;
  target: 'synthetic' | 'google_maps';
  method: 'deterministic_browser' | 'openai_agent' | 'manual' | 'streetview_static_api';
  worker_count: number;
  requests_per_minute: number;
  max_captures: number;
  max_duration_minutes: number;
  model_budget_usd: number;
  max_attempts: number;
  mock: Record<string, unknown>;
}

export interface TaskRow {
  id: string;
  job_id: string;
  sample_id: string;
  segment_key: string;
  ordinal: number;
  status: string;
  attempt_count: number;
  failure_count: number;
  max_attempts: number;
  next_attempt_at: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  last_error: string | null;
  outcome_reason: string | null;
  result: Json;
  view_filter: string[] | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskAttemptRow {
  attempt_no: number;
  worker_id: string;
  outcome: string;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  model_calls: number;
  actions: number;
}

export interface TaskDetail extends TaskRow {
  attempts: TaskAttemptRow[];
}

export interface JobEvent {
  id: number;
  created_at: string | null;
  level: string;
  kind: string;
  message: string;
  worker_id?: string | null;
  task_id?: string | null;
}

export interface WorkerCommandRow {
  id: number;
  command: string;
  payload: Json;
  status: string;
  result: Json;
  created_at: string | null;
  executed_at: string | null;
}

// ---------------------------------------------------------------- coverage
export interface CoverageSampleProps {
  id: string;
  ordinal: number;
  kind: string;
  task_id: string | null;
  task_status: string;
  coverage: string;
  views: Record<string, number>;
  attempts: number;
  reason: string | null;
}

export interface CameraProps {
  observation_id: string;
  pano_id: string | null;
  verification_status: string;
  association_status: string;
  side: string;
  heading_deg: number | null;
  label: string;
}

export interface Gap {
  segment_key: string;
  side: string;
  from_m: number;
  to_m: number;
  length_m: number;
}

export interface Coverage {
  job_id: string;
  collection: { tasks_total: number; tasks_terminal: number; completeness: number; task_states: Record<string, number> };
  views: Record<string, number>;
  verified_coverage: number;
  covered_including_reuse_and_review: number;
  association: Record<string, number>;
  by_segment_side: ({ segment_key: string; side: string } & Record<string, number | string>)[];
  gaps: Gap[];
  gap_threshold_m: number;
  distinct_panoramas: number;
  panoramas_shared_by_multiple_samples: number;
  samples_resolved_by_reuse: number;
  samples: FeatureCollection<CoverageSampleProps>;
  cameras: FeatureCollection<CameraProps>;
  notes: string[];
}

// ---------------------------------------------------------------- observations
export interface ObservationSummary {
  id: string;
  job_id: string;
  task_id: string;
  sample_id: string;
  sample_ordinal: number | null;
  segment_key: string | null;
  intended_side: string | null;
  view_kind: string | null;
  planned_heading_deg: number | null;
  heading_deg: number | null;
  pano_id: string | null;
  imagery_date: string | null;
  imagery_date_precision: string | null;
  camera_offset_m: number | null;
  verification_status: string;
  association_status: string;
  quality_flags: string[];
  review_status: string | null;
  is_current: boolean;
  capture_method: string | null;
  target: string | null;
  collected_at: string | null;
  thumb_url: string | null;
  image_url: string | null;
  width: number | null;
  height: number | null;
}

export interface ObservationList {
  total: number;
  items: ObservationSummary[];
}

export interface ObservationDetail {
  observation_id: string;
  project_id: string;
  job_id: string;
  task_id: string;
  attempt_id: string | null;
  worker_id: string | null;
  plan_id: string;
  segment_id: string;
  segment_key: string | null;
  sample_id: string;
  planned_view_id: string;
  intended_side: string | null;
  side_convention?: string;
  view_kind: string | null;
  requested_position: { lat: number; lng: number; meaning?: string } | null;
  camera_position: { lat: number; lng: number; provenance: string | null; meaning?: string } | null;
  camera_offset_m: number | null;
  panorama: { source_pano_id: string | null; provenance: string | null; note?: string } | null;
  source_url: string | null;
  orientation: {
    planned: { heading_deg: number | null; pitch_deg: number | null; fov_deg: number | null } | null;
    observed: {
      heading_deg: number | null;
      heading_provenance: string | null;
      pitch_deg: number | null;
      pitch_provenance: string | null;
      fov_deg: number | null;
      fov_provenance: string | null;
    } | null;
  } | null;
  viewport: { width: number | null; height: number | null; device_scale_factor: number | null } | null;
  imagery_date: { value: string | null; precision: string | null; provenance: string | null } | null;
  collected_at: string | null;
  image: { path: string | null; format: string | null; width: number | null; height: number | null; bytes: number | null; sha256: string | null } | null;
  capture_method: string | null;
  target: string | null;
  config_version_id: string | null;
  attribution: string | null;
  verification_status: string;
  association_status: string;
  quality_flags: string[] | null;
  review: { status: string | null; note: string | null; reviewed_at: string | null } | null;
  verification_evidence: Json;
  image_url: string | null;
  thumb_url: string | null;
  sample_ordinal: number | null;
  is_current: boolean;
  links: { planned_view_id: string; sample_id: string; kind: string; association_status: string; camera_to_sample_m: number | null; active: boolean }[] | null;
  crops: { id: string; x: number; y: number; width: number; height: number; purpose: string; url: string | null; raw_sha256: string }[] | null;
}

// ---------------------------------------------------------------- exports
export interface ExportRow {
  id: string;
  path: string;
  absolute_path: string;
  observation_count: number;
  manifest_sha256: string | null;
  created_at?: string | null;
  job_id?: string | null;
}

export interface RetentionPreview {
  cutoff: string;
  observations: number;
  bytes: number;
  retention_days: number;
}
