import type { PlanParams } from '../lib/types';

type Group = 'Sampling & views' | 'Geometry' | 'Capture policy';

interface FieldSpec {
  key: keyof PlanParams;
  label: string;
  group: Group;
  type: 'number' | 'int' | 'select' | 'bool';
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  options?: { value: string; label: string }[];
  help?: string;
  nullable?: boolean;
  testid?: string;
}

// Mirrors backend app/geo/planning.py PlanParams (defaults + constraints).
export const PLAN_DEFAULTS: PlanParams = {
  sampling_interval_m: 15,
  sides: 'both',
  views_per_side: 1,
  oblique_angle_deg: 45,
  pitch_deg: -5,
  fov_deg: 80,
  viewport_width: 1440,
  viewport_height: 900,
  footpath_offset_m: 7,
  segment_max_length_m: 120,
  turn_split_deg: 30,
  include_crossing_approaches: true,
  include_route_endpoints_as_nodes: true,
  crossing_approach_offset_m: 10,
  crossing_view_angle_deg: 35,
  polygon_mode: 'perimeter',
  max_imagery_age_months: 60,
  unknown_date_policy: 'needs_review',
  max_camera_offset_m: 20,
  no_imagery_distance_m: 60,
  heading_tolerance_deg: 10,
  reuse_panorama_views: true,
};

const FIELDS: FieldSpec[] = [
  { key: 'sampling_interval_m', label: 'Sampling interval', group: 'Sampling & views', type: 'number', min: 3, max: 500, step: 1, unit: 'm', testid: 'plan-interval', help: 'Distance between requested samples along each segment' },
  {
    key: 'sides',
    label: 'Sides',
    group: 'Sampling & views',
    type: 'select',
    testid: 'plan-sides',
    options: [
      { value: 'both', label: 'both' },
      { value: 'left', label: 'left' },
      { value: 'right', label: 'right' },
    ],
    help: 'Relative to each segment’s stored direction (arrow on the map)',
  },
  {
    key: 'views_per_side',
    label: 'Views per side',
    group: 'Sampling & views',
    type: 'select',
    testid: 'plan-views-per-side',
    options: [
      { value: '1', label: '1 — perpendicular' },
      { value: '2', label: '2 — + forward oblique' },
      { value: '3', label: '3 — + backward oblique' },
    ],
    help: '1 = perpendicular; 2 = + forward oblique; 3 = + backward oblique',
  },
  { key: 'oblique_angle_deg', label: 'Oblique angle', group: 'Sampling & views', type: 'number', min: 10, max: 80, step: 1, unit: '°', help: 'Offset of oblique views from perpendicular' },
  { key: 'pitch_deg', label: 'Pitch', group: 'Sampling & views', type: 'number', min: -90, max: 90, step: 1, unit: '°', help: 'Negative looks down' },
  { key: 'fov_deg', label: 'Field of view', group: 'Sampling & views', type: 'number', min: 10, max: 100, step: 1, unit: '°' },
  { key: 'viewport_width', label: 'Viewport width', group: 'Sampling & views', type: 'int', min: 320, max: 2560, step: 1, unit: 'px' },
  { key: 'viewport_height', label: 'Viewport height', group: 'Sampling & views', type: 'int', min: 240, max: 1600, step: 1, unit: 'px' },

  { key: 'footpath_offset_m', label: 'Footpath offset', group: 'Geometry', type: 'number', min: 1, max: 40, step: 0.5, unit: 'm', help: 'Approximate centreline-to-footpath distance (overlay only, not surveyed)' },
  { key: 'segment_max_length_m', label: 'Max segment length', group: 'Geometry', type: 'number', min: 10, max: 2000, step: 1, unit: 'm' },
  { key: 'turn_split_deg', label: 'Split at turns over', group: 'Geometry', type: 'number', min: 5, max: 170, step: 1, unit: '°' },
  { key: 'include_crossing_approaches', label: 'Include crossing approaches', group: 'Geometry', type: 'bool', help: 'Extra samples approaching candidate nodes' },
  { key: 'include_route_endpoints_as_nodes', label: 'Route endpoints as nodes', group: 'Geometry', type: 'bool' },
  { key: 'crossing_approach_offset_m', label: 'Crossing approach offset', group: 'Geometry', type: 'number', min: 2, max: 60, step: 0.5, unit: 'm' },
  { key: 'crossing_view_angle_deg', label: 'Crossing view angle', group: 'Geometry', type: 'number', min: 0, max: 80, step: 1, unit: '°' },
  {
    key: 'polygon_mode',
    label: 'Polygon mode',
    group: 'Geometry',
    type: 'select',
    options: [
      { value: 'perimeter', label: 'perimeter' },
      { value: 'clip_lines', label: 'clip_lines' },
    ],
    help: 'How polygon routes become lines',
  },

  { key: 'max_imagery_age_months', label: 'Max imagery age', group: 'Capture policy', type: 'int', min: 1, max: 600, step: 1, unit: 'months', nullable: true, help: 'Blank = no limit' },
  {
    key: 'unknown_date_policy',
    label: 'Unknown imagery date',
    group: 'Capture policy',
    type: 'select',
    options: [
      { value: 'needs_review', label: 'needs_review' },
      { value: 'accept_flagged', label: 'accept_flagged' },
      { value: 'reject', label: 'reject' },
    ],
  },
  { key: 'max_camera_offset_m', label: 'Max camera offset', group: 'Capture policy', type: 'number', min: 1, max: 200, step: 0.5, unit: 'm', help: 'Farther camera ⇒ association uncertain' },
  { key: 'no_imagery_distance_m', label: 'No-imagery distance', group: 'Capture policy', type: 'number', min: 5, max: 500, step: 1, unit: 'm', help: 'Farther camera ⇒ no imagery near sample (must be ≥ max camera offset)' },
  { key: 'heading_tolerance_deg', label: 'Heading tolerance', group: 'Capture policy', type: 'number', min: 1, max: 90, step: 0.5, unit: '°' },
  { key: 'reuse_panorama_views', label: 'Reuse panorama views', group: 'Capture policy', type: 'bool', help: 'Neighbouring samples served by the same panorama may reuse its captures' },
];

const GROUPS: Group[] = ['Sampling & views', 'Geometry', 'Capture policy'];

export type ParamForm = Record<string, string | boolean>;

export function paramsToForm(p: Partial<PlanParams> | null | undefined): ParamForm {
  const merged: PlanParams = { ...PLAN_DEFAULTS, ...(p ?? {}) };
  const out: ParamForm = {};
  for (const f of FIELDS) {
    const v = merged[f.key];
    if (f.type === 'bool') out[f.key] = !!v;
    else out[f.key] = v === null || v === undefined ? '' : String(v);
  }
  return out;
}

export function formToParams(form: ParamForm): { params: PlanParams | null; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  const out: Record<string, unknown> = {};
  for (const f of FIELDS) {
    const raw = form[f.key];
    if (f.type === 'bool') {
      out[f.key] = !!raw;
      continue;
    }
    if (f.type === 'select') {
      out[f.key] = f.key === 'views_per_side' ? Number(raw) : raw;
      continue;
    }
    const s = String(raw ?? '').trim();
    if (s === '') {
      if (f.nullable) {
        out[f.key] = null;
        continue;
      }
      errors[f.key] = 'Required';
      continue;
    }
    const n = Number(s);
    if (!Number.isFinite(n)) {
      errors[f.key] = 'Not a number';
      continue;
    }
    if (f.type === 'int' && !Number.isInteger(n)) {
      errors[f.key] = 'Whole number required';
      continue;
    }
    if (f.min !== undefined && n < f.min) errors[f.key] = `Minimum ${f.min}`;
    if (f.max !== undefined && n > f.max) errors[f.key] = `Maximum ${f.max}`;
    out[f.key] = n;
  }
  if (!errors.no_imagery_distance_m && !errors.max_camera_offset_m && Number(out.no_imagery_distance_m) < Number(out.max_camera_offset_m)) {
    errors.no_imagery_distance_m = 'Must be ≥ max camera offset';
  }
  if (Object.keys(errors).length) return { params: null, errors };
  return { params: out as unknown as PlanParams, errors };
}

export function PlanParamsForm({ form, onChange, errors, disabled }: { form: ParamForm; onChange: (f: ParamForm) => void; errors: Record<string, string>; disabled?: boolean }) {
  const set = (k: string, v: string | boolean) => onChange({ ...form, [k]: v });
  return (
    <div className="params-form">
      {GROUPS.map((g) => (
        <fieldset key={g} className="params-group" disabled={disabled}>
          <legend>{g}</legend>
          <div className="params-grid">
            {FIELDS.filter((f) => f.group === g).map((f) => {
              const id = `pp-${f.key}`;
              const err = errors[f.key];
              if (f.type === 'bool') {
                return (
                  <div key={f.key} className="field field-check" title={f.help}>
                    <label htmlFor={id}>
                      <input id={id} type="checkbox" checked={!!form[f.key]} onChange={(e) => set(f.key, e.target.checked)} data-testid={f.testid} />
                      {f.label}
                    </label>
                    {f.help && <div className="hint">{f.help}</div>}
                  </div>
                );
              }
              return (
                <div key={f.key} className={`field ${err ? 'field-error' : ''} ${f.key === 'views_per_side' ? 'field-wide' : ''}`}>
                  <label htmlFor={id}>
                    {f.label}
                    {f.unit && <span className="unit"> ({f.unit})</span>}
                  </label>
                  {f.type === 'select' ? (
                    <select id={id} value={String(form[f.key])} onChange={(e) => set(f.key, e.target.value)} data-testid={f.testid}>
                      {f.options!.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id={id}
                      type="number"
                      inputMode="decimal"
                      min={f.min}
                      max={f.max}
                      step={f.step}
                      value={String(form[f.key])}
                      placeholder={f.nullable ? 'no limit' : undefined}
                      onChange={(e) => set(f.key, e.target.value)}
                      data-testid={f.testid}
                      aria-invalid={!!err}
                    />
                  )}
                  {err ? <div className="field-msg">{err}</div> : f.help ? <div className="hint">{f.help}</div> : null}
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
