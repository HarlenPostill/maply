// Pure data model for the accessibility atlas: parsing, dimensions, filters and counting.
// No DOM access, so it runs under `node --experimental-strip-types` tests.
import { z } from 'zod';

// ---------------------------------------------------------------- snapshot

const ints = z.array(z.number().int());
const nums = z.array(z.number());
const strs = z.array(z.string());
const photoColumns = {
  id: strs, code: strs, segment: ints, street: ints, side: ints, status: ints, annotation: ints, lng: nums, lat: nums,
  position: ints, heading: ints, pano: ints, imagery_month: ints, imagery_date_type: ints, captured: ints,
  capture_source: ints, tagged: ints, model: ints, review_confirmation: ints, image_source: ints, review_reasons: ints,
  footpath: ints, footpath_visibility: ints, signals: ints,
};
export const snapshotSchema = z.object({
  version: z.literal(2), generated_at: z.string(), digest: z.string(), job_id: z.string(), job_name: z.string(), job_slug: z.string(),
  vocabulary: z.object({
    kinds: strs, visibility: strs, sides: strs, annotation: strs, presence: strs, metrics: strs, tag_names: strs,
    tag_values: z.record(z.string(), strs), kind_tags: z.record(z.string(), strs),
    status: strs, imagery_date_type: strs, capture_source: strs, model: strs, review_confirmation: strs,
    image_source: strs, highway: strs, osm_sidewalk: strs,
  }),
  streets: strs,
  segments: z.object({ key: strs, street: ints, length_m: nums, highway: ints, osm_sidewalk: ints, coordinates: z.array(z.array(z.tuple([z.number(), z.number()]))) }),
  photos: z.object(photoColumns),
  features: z.object({ photo: ints, kind: ints, tags: z.array(ints) }),
  pano_count: z.number().int().nonnegative(),
});
export const metaSchema = z.object({ version: z.number(), job_id: z.string(), digest: z.string(), generated_at: z.string(), checked_at: z.string() });
export type Snapshot = z.infer<typeof snapshotSchema>;
export type Meta = z.infer<typeof metaSchema>;
type Photos = Snapshot['photos'];

export type Dataset = {
  snapshot: Snapshot; n: number; p: Photos; vocab: Snapshot['vocabulary'];
  /** Number of planned photos sharing each photo's panorama (0 when no panorama id). */
  panoSize: Int32Array;
  /** features of photo i are featStart[i]..featStart[i+1]-1 */
  featStart: Int32Array; fn: number; fPhoto: Int32Array; fKind: Int8Array;
  tagStart: Int32Array; tName: Uint8Array; tValue: Uint8Array; tVis: Uint8Array; tConf: Uint8Array;
  /** Lowest model-reported confidence (0–100) across known feature tags; -1 when none. */
  minConf: Int16Array;
  /** 0 every tag clear, 1 some partial/blurred, 2 some occluded/outside frame, -1 no feature tags. */
  worstVis: Int8Array;
  segCount: number; segPhotoCount: Int32Array;
  tagIndex: Map<string, number>;
  kindIndex: Map<string, number>;
  monthRange: [number, number] | null;
};

export function parseSnapshot(json: unknown): Snapshot {
  const parsed = snapshotSchema.parse(json);
  const n = parsed.photos.id.length;
  for (const [key, column] of Object.entries(parsed.photos)) if (column.length !== n) throw new Error(`Snapshot column ${key} is incomplete`);
  const f = parsed.features;
  if (f.kind.length !== f.photo.length || f.tags.length !== f.photo.length) throw new Error('Snapshot feature columns are incomplete');
  const s = parsed.segments;
  if ([s.street, s.length_m, s.highway, s.osm_sidewalk, s.coordinates].some((column) => column.length !== s.key.length)) throw new Error('Snapshot segment columns are incomplete');
  return parsed;
}

export function buildDataset(snapshot: Snapshot): Dataset {
  const p = snapshot.photos, n = p.id.length, f = snapshot.features, fn = f.photo.length;
  const panoCounts = new Int32Array(snapshot.pano_count);
  for (const pano of p.pano) if (pano >= 0) panoCounts[pano]++;
  const panoSize = new Int32Array(n);
  for (let i = 0; i < n; i++) panoSize[i] = p.pano[i] >= 0 ? panoCounts[p.pano[i]] : 0;
  // Features are written in photo order; sort defensively so offsets are valid.
  let order = Array.from({ length: fn }, (_, i) => i);
  if (f.photo.some((value, i) => i > 0 && value < f.photo[i - 1])) order = order.sort((a, b) => f.photo[a] - f.photo[b]);
  const fPhoto = new Int32Array(fn), fKind = new Int8Array(fn), tagStart = new Int32Array(fn + 1);
  let tags = 0;
  order.forEach((source, i) => { fPhoto[i] = f.photo[source]; fKind[i] = f.kind[source]; tagStart[i] = tags; tags += f.tags[source].length; });
  tagStart[fn] = tags;
  const tName = new Uint8Array(tags), tValue = new Uint8Array(tags), tVis = new Uint8Array(tags), tConf = new Uint8Array(tags);
  order.forEach((source, i) => {
    let t = tagStart[i];
    for (let packed of f.tags[source]) {
      tConf[t] = packed % 128; packed = Math.floor(packed / 128);
      tVis[t] = packed % 8; packed = Math.floor(packed / 8);
      tValue[t] = packed % 32; tName[t] = Math.floor(packed / 32); t++;
    }
  });
  const featStart = new Int32Array(n + 1);
  for (let i = 0, j = 0; i <= n; i++) { while (j < fn && fPhoto[j] < i) j++; featStart[i] = j; }
  const minConf = new Int16Array(n).fill(-1), worstVis = new Int8Array(n).fill(-1);
  const visibility = snapshot.vocabulary.visibility;
  const unknownValue = new Map(snapshot.vocabulary.tag_names.map((name, i) => [i, (snapshot.vocabulary.tag_values[name] ?? []).indexOf('unknown')]));
  for (let fi = 0; fi < fn; fi++) {
    const i = fPhoto[fi];
    for (let t = tagStart[fi]; t < tagStart[fi + 1]; t++) {
      const vis = visibility[tVis[t]] ?? 'unknown';
      const level = vis === 'clear' ? 0 : vis === 'partial' || vis === 'blurred' ? 1 : 2;
      if (level > worstVis[i]) worstVis[i] = level;
      if (tConf[t] !== 127 && tValue[t] !== unknownValue.get(tName[t]) && (minConf[i] < 0 || tConf[t] < minConf[i])) minConf[i] = tConf[t];
    }
  }
  const segCount = snapshot.segments.key.length, segPhotoCount = new Int32Array(segCount);
  for (const seg of p.segment) if (seg >= 0 && seg < segCount) segPhotoCount[seg]++;
  let lo = Infinity, hi = -Infinity;
  for (const month of p.imagery_month) if (month >= 0) { lo = Math.min(lo, month); hi = Math.max(hi, month); }
  return {
    snapshot, n, p, vocab: snapshot.vocabulary, panoSize, featStart, fn, fPhoto, fKind, tagStart, tName, tValue, tVis, tConf,
    minConf, worstVis, segCount, segPhotoCount,
    tagIndex: new Map(snapshot.vocabulary.tag_names.map((name, i) => [name, i])),
    kindIndex: new Map(snapshot.vocabulary.kinds.map((name, i) => [name, i])),
    monthRange: Number.isFinite(lo) ? [lo, hi] : null,
  };
}

// ---------------------------------------------------------------- words & formatting

const SPECIAL: Record<string, string> = {
  continuous_visible_extent: 'Continuous (visible extent)', pavers_unspecified: 'Pavers (unspecified)', apparent_flush: 'Apparently flush',
  apparently_level: 'Apparently level', not_needed_level_transition: 'Not needed (level transition)', none_visible: 'None visible',
  level_or_raised_crossing: 'Level or raised crossing', diagonally_into_junction: 'Diagonally into junction', visible_space: 'Visible space',
  apparently_restricted: 'Apparently restricted', ends_in_road: 'Ends in road', outside_route: 'Outside route',
  beside_route: 'Beside route', on_route: 'On route', partially_encroaching: 'Partially encroaching', kerb_ramp: 'Kerb ramp',
  footpath_section: 'Footpath section', no_imagery: 'No imagery', needs_check: 'Needs check', outside_frame: 'Outside frame',
  living_street: 'Living street', checksum_verified_local_backup: 'Restored from verified backup',
};
export function words(value: string): string {
  const text = SPECIAL[value] ?? value.replaceAll('_', ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}
export const KIND_PLURAL: Record<string, string> = {
  footpath_section: 'footpath sections', driveway: 'driveways', kerb_ramp: 'kerb ramps', crossing: 'crossings', hazard: 'hazards',
};
export const fmt = (value: number) => value.toLocaleString('en-AU');
export function percent(rate: number | null | undefined, digits = 0): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return '—';
  const floor = 10 ** -digits;
  if (rate > 0 && rate * 100 < floor) return `<${floor.toFixed(digits)}%`;
  if (rate < 1 && rate * 100 > 100 - floor) return `>${(100 - floor).toFixed(digits)}%`;
  return `${(rate * 100).toFixed(digits)}%`;
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export const monthLabel = (month: number) => `${MONTHS[month % 12]} ${Math.floor(month / 12)}`;
export const monthIso = (month: number) => `${Math.floor(month / 12)}-${String(month % 12 + 1).padStart(2, '0')}`;
export const minuteDate = (minutes: number) => new Date(minutes * 60000);
export const DAY = 1440;

// ---------------------------------------------------------------- colour

export const PALETTE = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];
export const OTHER = '#9b8c7d';
export const UNKNOWN = '#8f9a9f';
export const NOT_RECORDED = '#c5ccd0';
export const ABSENT = '#e2e6e8';
export const CONCERN = '#d03b3b';
export const NO_CONCERN = '#2a78d6';
const RAMP = ['#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b'];
export function ramp(position: number): string {
  const x = Math.max(0, Math.min(1, position)) * (RAMP.length - 1);
  const a = RAMP[Math.floor(x)], b = RAMP[Math.ceil(x)], t = x - Math.floor(x);
  const channel = (hex: string, i: number) => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16);
  return `#${[0, 1, 2].map((i) => Math.round(channel(a, i) + (channel(b, i) - channel(a, i)) * t).toString(16).padStart(2, '0')).join('')}`;
}
/** Sequential blue for magnitude in matrices/heat cells; 0 maps to the near-surface step. */
export function heat(position: number): string {
  const steps = ['#eef4fc', '#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'];
  const x = Math.max(0, Math.min(1, position)) * (steps.length - 1);
  return steps[Math.round(x)];
}

// ---------------------------------------------------------------- dimensions

export type Category = { key: string; label: string; color: string; structural: boolean };
export type DimData = { single?: Int16Array; mask?: Uint32Array; feature?: Int8Array };
export type Dimension = {
  id: string; label: string; group: string; description: string;
  level: 'photo' | 'feature'; kind: number; ordinal: boolean; colorable: boolean;
  /** What one feature-unit count is, e.g. “hazards”. */
  noun: string;
  categories: Category[];
  data: () => DimData;
  /** Category indices in colour/selection priority: rarer known values first, structural last. */
  priority: () => number[];
};
export type Unit = 'photos' | 'panoramas' | 'segments' | 'features';
export const UNITS: { key: Unit; label: string; note: string }[] = [
  { key: 'photos', label: 'Photos', note: 'Each planned photo counts once per category it shows.' },
  { key: 'panoramas', label: 'Panoramas', note: 'Photos from the same Street View panorama count once, removing duplicate overlapping imagery.' },
  { key: 'segments', label: 'Segments', note: 'Planned street segments with at least one matching photo.' },
  { key: 'features', label: 'Features', note: 'Individual annotated features (one photo can contain several). Applies to feature attributes.' },
];

export const METRIC_INFO: Record<string, { name: string; rule: string }> = {
  quality: { name: 'Visible footpath concern', rule: 'Footpath present and a visible section shows a defect, rough or very rough texture, loose/soft surface (gravel, earth, sand, grass, rounded cobbles), interruption or end. “No concern recorded” needs every visible section to carry a clear no-visible-defect condition; concrete alone never counts. This is a rule over existing tags, not a grade or severity.' },
  passage: { name: 'Narrowed or blocked passage', rule: 'Any on-route feature with clear passage narrowed or blocked. Assessed when any known clear-passage tag exists.' },
  surface: { name: 'Surface concern', rule: 'Visible defect, rough/very rough texture, or loose/soft surface type on an on-route feature. Assessed when any known condition, texture or surface tag exists, so a plain material counts as assessed with no concern.' },
  slope: { name: 'Apparent moderate/steep slope', rule: 'Moderate or steep cross, longitudinal or ramp slope. Visual estimates, not measurements.' },
  crossing: { name: 'Crossing transition concern', rule: 'Missing kerb ramp at a crossing approach, unmodified kerb, steps, or a raised lip/step/gap/channel at a ramp join.' },
  footpath: { name: 'Missing footpath', rule: 'Overall footpath labelled absent with clear visibility.' },
  hazards: { name: 'On-route hazard', rule: 'A hazard on or partially encroaching the route. Without one the photo shows “not assessed” rather than “no hazard”, because hazard absence is never asserted.' },
};

const STATUS_LABEL: Record<string, string> = {
  current: 'Annotated (current image)', awaiting: 'Saved, awaiting annotation', stale: 'Annotation out of date',
};
const STATUS_COLOR: Record<string, string> = {
  current: '#2a78d6', awaiting: '#eda100', stale: '#e87ba4', pending: '#c5ccd0', no_imagery: '#9b8c7d', blocked: '#6b5d52', skipped: '#b6a99c', retake: '#8f9a9f',
};
const QUIET = new Set(['not_recorded', 'not_assessed', 'undated', 'no_known_tags', 'no_feature_tags', 'not_tagged', 'no_pano', 'no_capture_time']);
const ORDINAL_TAGS = new Set(['cross_slope', 'longitudinal_slope', 'slope', 'texture', 'clear_passage']);
const DAY_MINUTES = 1440;

type Spec = {
  id: string; label: string; group: string; description: string; ordinal?: boolean; colorable?: boolean;
  values: string[]; labels?: string[]; colors?: string[]; structural?: string[];
  code: (i: number) => number;
};

function makeCategories(values: string[], labels: string[] | undefined, colors: string[] | undefined, structural: Set<string>, ordinal: boolean, counts: () => Float64Array): () => Category[] {
  return () => {
    const known = values.map((value, i) => ({ value, i })).filter(({ value }) => !structural.has(value));
    const slot = new Map<number, string>();
    if (colors) known.forEach(({ i }) => slot.set(i, colors[i] ?? OTHER));
    else if (ordinal) known.forEach(({ i }, k) => slot.set(i, ramp(known.length === 1 ? .5 : k / (known.length - 1))));
    else if (known.length <= PALETTE.length) known.forEach(({ i }, k) => slot.set(i, PALETTE[k]));
    else {
      // More than eight identities: colour the seven most frequent (fixed for this snapshot) and fold the rest into Other.
      const total = counts();
      const top = [...known].sort((a, b) => total[b.i] - total[a.i]).slice(0, PALETTE.length - 1).map(({ i }) => i).sort((a, b) => a - b);
      top.forEach((i, k) => slot.set(i, PALETTE[k]));
    }
    return values.map((value, i) => ({
      key: value, label: labels?.[i] ?? words(value), structural: structural.has(value),
      color: slot.get(i) ?? (value === 'unknown' ? UNKNOWN : QUIET.has(value) ? NOT_RECORDED : structural.has(value) ? ABSENT : OTHER),
    }));
  };
}

function lazy<T>(make: () => T): () => T { let value: T | undefined; return () => (value ??= make()); }

export function buildDimensions(ds: Dataset): Dimension[] {
  const { p, vocab, n } = ds;
  const out: Dimension[] = [];
  const annotated = (i: number) => p.annotation[i] === 1;
  const singleDim = (spec: Spec) => {
    const data = lazy(() => { const single = new Int16Array(n); for (let i = 0; i < n; i++) single[i] = spec.code(i); return { single }; });
    const globalCounts = lazy(() => { const c = new Float64Array(spec.values.length); const { single } = data(); for (let i = 0; i < n; i++) if (single![i] >= 0) c[single![i]]++; return c; });
    const structural = new Set(spec.structural ?? []);
    const categories = makeCategories(spec.values, spec.labels, spec.colors, structural, !!spec.ordinal, globalCounts)();
    out.push({
      id: spec.id, label: spec.label, group: spec.group, description: spec.description, level: 'photo', kind: -1, noun: 'photos',
      ordinal: !!spec.ordinal, colorable: spec.colorable !== false, categories, data,
      priority: lazy(() => priorityOf(categories, globalCounts())),
    });
  };

  // --- Pipeline & annotation
  const statusValues = ['current', 'awaiting', 'stale', ...vocab.status.filter((s) => !['captured', 'accepted', 'needs_check'].includes(s))];
  singleDim({
    id: 'annotation', label: 'Photo & annotation status', group: 'Coverage', values: statusValues,
    labels: statusValues.map((v) => STATUS_LABEL[v] ?? words(v)),
    colors: statusValues.map((v) => STATUS_COLOR[v] ?? ABSENT),
    structural: [],
    description: 'Every planned photo: annotated against its current image, saved but not yet annotated, annotation for an older image, or not saved (and why).',
    code: (i) => {
      if (p.annotation[i] === 1) return 0;
      const status = vocab.status[p.status[i]];
      if (status === 'captured' || status === 'accepted' || status === 'needs_check') return p.annotation[i] === 2 ? 2 : 1;
      const index = statusValues.indexOf(status ?? '');
      return index >= 0 ? index : 1;
    },
  });
  singleDim({
    id: 'footpath', label: 'Footpath presence', group: 'Footpath', values: ['present', 'absent', 'unknown', 'not_annotated'],
    colors: ['#2a78d6', '#eb6834', UNKNOWN, ABSENT], structural: ['unknown', 'not_annotated'],
    description: 'The overall footpath observation for each annotated photo. Absence claims require clear visibility.',
    code: (i) => annotated(i) ? Math.max(0, p.footpath[i]) : 3,
  });
  const qualityMetric = vocab.metrics.indexOf('quality');
  singleDim({
    id: 'footpath_quality', label: 'Footpath quality', group: 'Footpath',
    values: ['no_visible_defect', 'concern', 'not_assessed', 'no_footpath', 'not_annotated'],
    labels: ['No visible defect reported', 'Visible quality concern', 'Quality not assessed', 'No footpath', 'Not annotated'],
    colors: [NO_CONCERN, CONCERN, NOT_RECORDED, '#72549a', ABSENT],
    structural: ['not_assessed', 'no_footpath', 'not_annotated'],
    description: `Observed quality, not a severity grade. ${METRIC_INFO.quality.rule} Missing footpaths and unassessed quality remain separate.`,
    code: (i) => {
      if (!annotated(i)) return 4;
      if (p.footpath[i] === vocab.presence.indexOf('absent')) return 3;
      if (qualityMetric < 0 || p.signals[i] < 0) return 2;
      const signal = (p.signals[i] >> (2 * qualityMetric)) & 3;
      return signal === 2 ? 1 : signal === 1 ? 0 : 2;
    },
  });
  const sides = ['left', 'right', 'none'];
  singleDim({ id: 'side', label: 'Planned street side', group: 'Place', values: sides, labels: ['Left side', 'Right side', 'Side unspecified'],
    description: 'Relative to the planned segment direction.', code: (i) => p.side[i] });
  const highway = [...vocab.highway, 'not_tagged'];
  singleDim({ id: 'highway', label: 'Road class (OSM)', group: 'Place', values: highway, structural: ['not_tagged'],
    description: 'OpenStreetMap highway class of the planned segment. Context only, not observed from imagery.',
    code: (i) => { const h = ds.snapshot.segments.highway[p.segment[i]]; return h >= 0 ? h : highway.length - 1; } });
  const sidewalk = [...vocab.osm_sidewalk, 'not_tagged'];
  singleDim({ id: 'osm_sidewalk', label: 'OSM sidewalk tag (segment)', group: 'Place', values: sidewalk, structural: ['not_tagged'],
    labels: sidewalk.map((v) => v === 'not_tagged' ? 'Not tagged in OSM' : `sidewalk=${v}`),
    description: 'The OpenStreetMap sidewalk tag on the planned road segment, for comparison with observed footpaths. OSM side naming follows way direction, so it is shown per segment, not per photo side.',
    code: (i) => { const s = ds.snapshot.segments.osm_sidewalk[p.segment[i]]; return s >= 0 ? s : sidewalk.length - 1; } });
  const streetValues = ds.snapshot.streets;
  singleDim({ id: 'street', label: 'Street', group: 'Place', values: streetValues, labels: streetValues, colorable: false,
    description: 'Street name of the planned photo.', code: (i) => p.street[i] });

  // --- Dates (never substituted for one another)
  const range = ds.monthRange;
  const firstYear = range ? Math.floor(range[0] / 12) : 0, lastYear = range ? Math.floor(range[1] / 12) : -1;
  const years = Array.from({ length: Math.max(0, lastYear - firstYear + 1) }, (_, k) => String(firstYear + k));
  const notAvailable = vocab.imagery_date_type.indexOf('not_available');
  const saved = (i: number) => ['captured', 'accepted', 'needs_check'].includes(vocab.status[p.status[i]] ?? '');
  singleDim({ id: 'imagery_year', label: 'Imagery year', group: 'Dates', ordinal: true, values: [...years, 'undated', 'not_saved'],
    labels: [...years, 'Undated imagery', 'No saved image'], structural: ['undated', 'not_saved'],
    description: 'Year of the provider-reported Street View imagery month. Undated imagery stays separate; capture time is never used instead.',
    code: (i) => !saved(i) ? years.length + 1 : p.imagery_month[i] >= 0 ? Math.floor(p.imagery_month[i] / 12) - firstYear : years.length });
  singleDim({ id: 'imagery_date_status', label: 'Imagery date status', group: 'Dates', values: ['dated', 'provider_no_date', 'not_recorded', 'not_saved'],
    labels: ['Imagery month recorded', 'Provider reported no date', 'No date recorded', 'No saved image'], colors: ['#2a78d6', '#eb6834', NOT_RECORDED, ABSENT],
    structural: ['not_saved'], description: 'Whether the saved capture recorded the provider imagery month.',
    code: (i) => !saved(i) ? 3 : p.imagery_month[i] >= 0 ? 0 : p.imagery_date_type[i] === notAvailable && notAvailable >= 0 ? 1 : 2 });
  const ages = ['lt1', '1to2', '2to4', '4to6', '6to10', '10plus', 'undated', 'no_capture_time'];
  singleDim({ id: 'imagery_age', label: 'Imagery age at capture', group: 'Dates', ordinal: true, values: ages,
    labels: ['Under 1 year', '1–2 years', '2–4 years', '4–6 years', '6–10 years', '10+ years', 'Undated imagery', 'No capture time / not saved'],
    structural: ['undated', 'no_capture_time'],
    description: 'Time between the imagery month (taken as mid-month) and when the screenshot was captured. Shows how old the depicted conditions may be.',
    code: (i) => {
      if (!saved(i) || p.captured[i] < 0) return 7;
      if (p.imagery_month[i] < 0) return 6;
      const age = imageryAgeYears(p.imagery_month[i], p.captured[i]);
      return age < 1 ? 0 : age < 2 ? 1 : age < 4 ? 2 : age < 6 ? 3 : age < 10 ? 4 : 5;
    } });
  const sources = [...vocab.capture_source, 'not_recorded'];
  singleDim({ id: 'capture_source', label: 'Capture source', group: 'Dates', values: sources, structural: ['not_recorded'],
    description: 'How the screenshot was captured.', code: (i) => p.capture_source[i] >= 0 ? p.capture_source[i] : sources.length - 1 });

  // --- Uncertainty & review
  const models = [...vocab.model, 'not_annotated'];
  singleDim({ id: 'model', label: 'Annotation model', group: 'Review & uncertainty', values: models, labels: models.map((m) => m === 'not_annotated' ? 'Not annotated' : m), structural: ['not_annotated'],
    description: 'Model that produced the current annotation.', code: (i) => annotated(i) && p.model[i] >= 0 ? p.model[i] : models.length - 1 });
  singleDim({ id: 'review_notes', label: 'Review notes per photo', group: 'Review & uncertainty', ordinal: true,
    values: ['0', '1', '2-3', '4-9', '10+', 'not_annotated'], labels: ['None', '1 note', '2–3 notes', '4–9 notes', '10+ notes', 'Not annotated'], structural: ['not_annotated'],
    description: 'Count of model and deterministic review reasons (unknowns, low confidence, non-clear evidence, potential barriers).',
    code: (i) => { if (!annotated(i)) return 5; const r = p.review_reasons[i]; return r <= 0 ? 0 : r === 1 ? 1 : r <= 3 ? 2 : r <= 9 ? 3 : 4; } });
  const confirmation = ['automatic', 'human', 'model_only', 'not_annotated'];
  singleDim({ id: 'review_confirmation', label: 'Review confirmation', group: 'Review & uncertainty', values: confirmation,
    labels: ['Second-pass review, auto-accepted', 'Human confirmed', 'Model annotation only', 'Not annotated'], structural: ['not_annotated'],
    colors: ['#eda100', '#008300', '#2a78d6', ABSENT],
    description: 'Provenance of the current annotation. Automatic acceptance is recorded honestly and is not human verification.',
    code: (i) => { if (!annotated(i)) return 3; const c = vocab.review_confirmation[p.review_confirmation[i]]; return c === 'automatic' ? 0 : c === 'human' ? 1 : 2; } });
  singleDim({ id: 'min_confidence', label: 'Lowest model confidence', group: 'Review & uncertainty', ordinal: true,
    values: ['90plus', '80to89', '60to79', 'lt60', 'no_known_tags', 'not_annotated'], labels: ['90–100%', '80–89%', '60–79%', 'Below 60%', 'No known feature tags', 'Not annotated'],
    structural: ['no_known_tags', 'not_annotated'],
    description: 'Lowest model-reported confidence among the photo’s known feature tags. Uncalibrated; not a probability of accessibility.',
    code: (i) => { if (!annotated(i)) return 5; const c = ds.minConf[i]; return c < 0 ? 4 : c >= 90 ? 0 : c >= 80 ? 1 : c >= 60 ? 2 : 3; } });
  singleDim({ id: 'evidence_visibility', label: 'Evidence visibility', group: 'Review & uncertainty',
    values: ['all_clear', 'partial', 'hidden', 'no_feature_tags', 'not_annotated'], labels: ['All tags clearly visible', 'Some partial or blurred', 'Some occluded / outside frame', 'No feature tags', 'Not annotated'],
    colors: ['#2a78d6', '#eda100', '#eb6834', NOT_RECORDED, ABSENT], structural: ['no_feature_tags', 'not_annotated'],
    description: 'Worst visibility among the photo’s feature tags.',
    code: (i) => !annotated(i) ? 4 : ds.worstVis[i] < 0 ? 3 : ds.worstVis[i] });
  singleDim({ id: 'pano_shared', label: 'Shared panorama', group: 'Review & uncertainty', values: ['unique', 'two', 'three_plus', 'no_pano'],
    labels: ['Only photo from its panorama', '2 photos share it', '3+ photos share it', 'No panorama id'], structural: ['no_pano'], ordinal: true,
    description: 'Overlapping imagery: several planned photos can come from one Street View panorama and may show the same feature.',
    code: (i) => ds.panoSize[i] === 0 ? 3 : ds.panoSize[i] === 1 ? 0 : ds.panoSize[i] === 2 ? 1 : 2 });
  singleDim({ id: 'position', label: 'Map position source', group: 'Review & uncertainty', values: ['camera', 'planned'], labels: ['Observed camera position', 'Planned position (fallback)'],
    description: 'Neither locates the exact depicted feature.', code: (i) => p.position[i] });

  // --- Derived concern indicators
  vocab.metrics.forEach((metric, m) => {
    singleDim({ id: `metric:${metric}`, label: METRIC_INFO[metric]?.name ?? words(metric), group: 'Derived concern indicators',
      values: ['concern', 'no_concern', 'not_assessed', 'not_annotated'], labels: ['Concern recorded', 'No concern recorded', 'Not assessed', 'Not annotated'],
      colors: [CONCERN, NO_CONCERN, NOT_RECORDED, ABSENT], structural: ['not_assessed', 'not_annotated'],
      description: METRIC_INFO[metric]?.rule ?? '',
      code: (i) => { const s = p.signals[i]; if (!annotated(i) || s < 0) return 3; const v = (s >> (2 * m)) & 3; return v === 2 ? 0 : v === 1 ? 1 : 2; } });
  });

  // --- Feature kinds present (multi-valued)
  {
    const kinds = vocab.kinds, values = [...kinds, 'no_features', 'not_annotated'];
    const data = lazy(() => {
      const mask = new Uint32Array(n);
      for (let i = 0; i < n; i++) {
        if (!annotated(i)) { mask[i] = 1 << (kinds.length + 1); continue; }
        let m = 0;
        for (let f = ds.featStart[i]; f < ds.featStart[i + 1]; f++) m |= 1 << ds.fKind[f];
        mask[i] = m || 1 << kinds.length;
      }
      const feature = new Int8Array(ds.fn);
      for (let f = 0; f < ds.fn; f++) feature[f] = ds.fKind[f];
      return { mask, feature };
    });
    const counts = lazy(() => maskCounts(data().mask!, values.length));
    const categories = makeCategories(values, values.map((v) => v === 'no_features' ? 'No features (footpath absent/unknown)' : v === 'not_annotated' ? 'Not annotated' : words(v)), undefined, new Set(['no_features', 'not_annotated']), false, counts)();
    out.push({ id: 'feature_kind', label: 'Feature types present', group: 'Features', level: 'feature', kind: -2, noun: 'features', ordinal: false, colorable: true,
      description: 'Annotated feature kinds. A photo with a footpath section and a driveway counts in both.', categories, data,
      priority: lazy(() => priorityOf(categories, counts())) });
  }

  // --- One dimension per feature kind × tag (multi-valued at photo level)
  vocab.kinds.forEach((kind, k) => {
    for (const name of vocab.kind_tags[kind] ?? []) {
      const t = ds.tagIndex.get(name);
      if (t === undefined) continue;
      const tagValues = vocab.tag_values[name] ?? [];
      const values = [...tagValues, 'not_recorded', `no_${kind}`, 'not_annotated'];
      const notRecorded = tagValues.length, noKind = notRecorded + 1, notAnnotated = notRecorded + 2;
      if (values.length > 32) continue;
      const data = lazy(() => {
        const mask = new Uint32Array(n), feature = new Int8Array(ds.fn).fill(-1);
        for (let f = 0; f < ds.fn; f++) {
          if (ds.fKind[f] !== k) continue;
          let code = notRecorded;
          for (let x = ds.tagStart[f]; x < ds.tagStart[f + 1]; x++) if (ds.tName[x] === t) { code = ds.tValue[x]; break; }
          feature[f] = code;
          mask[ds.fPhoto[f]] |= 1 << code;
        }
        for (let i = 0; i < n; i++) if (!mask[i]) mask[i] = 1 << (annotated(i) ? noKind : notAnnotated);
        return { mask, feature };
      });
      const counts = lazy(() => featureCounts(data().feature!, values.length));
      const plural = KIND_PLURAL[kind] ?? kind;
      const labels = values.map((v, i) => i === notRecorded ? 'Not recorded' : i === noKind ? `No ${plural} annotated` : i === notAnnotated ? 'Photo not annotated' : words(v));
      const categories = makeCategories(values, labels, undefined, new Set(['unknown', 'not_recorded', `no_${kind}`, 'not_annotated']), ORDINAL_TAGS.has(name), counts)();
      out.push({
        id: `tag:${kind}:${name}`, label: `${words(kind)} · ${words(name).toLowerCase()}`, group: `${words(kind)} attributes`, level: 'feature', kind: k, noun: plural,
        ordinal: ORDINAL_TAGS.has(name), colorable: true, categories, data,
        description: `The ${words(name).toLowerCase()} tag on annotated ${plural}. “Not recorded” means the model did not emit this tag for that feature; “unknown” means it was emitted but could not be determined.`,
        priority: lazy(() => priorityOf(categories, counts())),
      });
    }
  });
  return out;
}

export function imageryAgeYears(month: number, capturedMinutes: number): number {
  const imagery = Date.UTC(Math.floor(month / 12), month % 12, 15);
  return (capturedMinutes * 60000 - imagery) / (365.25 * DAY_MINUTES * 60000);
}
function maskCounts(mask: Uint32Array, size: number) {
  const c = new Float64Array(size);
  for (const m of mask) for (let k = 0; k < size; k++) if (m & (1 << k)) c[k]++;
  return c;
}
function featureCounts(feature: Int8Array, size: number) {
  const c = new Float64Array(size);
  for (const code of feature) if (code >= 0) c[code]++;
  return c;
}
function priorityOf(categories: Category[], counts: Float64Array) {
  return categories.map((c, i) => ({ c, i })).sort((a, b) => Number(a.c.structural) - Number(b.c.structural) || counts[a.i] - counts[b.i] || a.i - b.i).map(({ i }) => i);
}

/** Every category index a photo falls in for a dimension. */
export function photoCodes(dim: Dimension, i: number): number[] {
  const d = dim.data();
  if (d.single) return d.single[i] >= 0 ? [d.single[i]] : [];
  const out: number[] = [], m = d.mask![i];
  for (let k = 0; k < dim.categories.length; k++) if (m & (1 << k)) out.push(k);
  return out;
}
/** One representative category for colouring a photo: its only value, or the rarest known value it shows. */
export function photoCategory(dim: Dimension, i: number): number {
  const d = dim.data();
  if (d.single) return d.single[i];
  const m = d.mask![i];
  for (const k of dim.priority()) if (m & (1 << k)) return k;
  return -1;
}

// ---------------------------------------------------------------- filters

export type Filters = {
  /** dimension id → allowed category keys (OR within a dimension, AND across dimensions) */
  cats: Record<string, string[]>;
  imagery?: [number, number]; captured?: [number, number]; tagged?: [number, number];
  segment?: number;
};
export const emptyFilters = (): Filters => ({ cats: {} });
export type Selection = { photo: Uint8Array; feature: Uint8Array; photos: number; features: number };

export function applyFilters(ds: Dataset, dims: Map<string, Dimension>, filters: Filters): Selection {
  const n = ds.n, photo = new Uint8Array(n).fill(1);
  const featureRules: { kind: number; feature: Int8Array; allowed: Uint8Array }[] = [];
  for (const [id, keys] of Object.entries(filters.cats)) {
    const dim = dims.get(id);
    if (!dim || !keys.length) continue;
    const allowed = new Uint8Array(dim.categories.length);
    let bits = 0;
    dim.categories.forEach((c, k) => { if (keys.includes(c.key)) { allowed[k] = 1; if (k < 32) bits |= 1 << k; } });
    const d = dim.data();
    if (d.single) { for (let i = 0; i < n; i++) if (photo[i] && !(d.single[i] >= 0 && allowed[d.single[i]])) photo[i] = 0; }
    else { const mask = d.mask!; for (let i = 0; i < n; i++) if (photo[i] && !(mask[i] & bits)) photo[i] = 0; }
    if (d.feature) featureRules.push({ kind: dim.kind, feature: d.feature, allowed });
  }
  const range = (values: number[], bounds?: [number, number]) => {
    if (!bounds) return;
    for (let i = 0; i < n; i++) if (photo[i] && !(values[i] >= 0 && values[i] >= bounds[0] && values[i] <= bounds[1])) photo[i] = 0;
  };
  range(ds.p.imagery_month, filters.imagery);
  range(ds.p.captured, filters.captured);
  range(ds.p.tagged, filters.tagged);
  if (filters.segment !== undefined) for (let i = 0; i < n; i++) if (ds.p.segment[i] !== filters.segment) photo[i] = 0;
  let photos = 0;
  for (let i = 0; i < n; i++) photos += photo[i];
  const feature = new Uint8Array(ds.fn);
  let features = 0;
  for (let f = 0; f < ds.fn; f++) {
    if (!photo[ds.fPhoto[f]]) continue;
    let ok = 1;
    // A filter on an attribute of this feature kind applies to each feature individually.
    for (const rule of featureRules) if ((rule.kind === ds.fKind[f] || rule.kind === -2) && !(rule.feature[f] >= 0 && rule.allowed[rule.feature[f]])) { ok = 0; break; }
    feature[f] = ok; features += ok;
  }
  return { photo, feature, photos, features };
}

export function filterCount(filters: Filters) {
  return Object.values(filters.cats).filter((v) => v.length).length + Number(!!filters.imagery) + Number(!!filters.captured) + Number(!!filters.tagged) + Number(filters.segment !== undefined);
}
export function toggleCategory(filters: Filters, dim: string, key: string, only = false): Filters {
  const current = filters.cats[dim] ?? [];
  const next = only ? (current.length === 1 && current[0] === key ? [] : [key]) : current.includes(key) ? current.filter((k) => k !== key) : [...current, key];
  const cats = { ...filters.cats, [dim]: next };
  if (!next.length) delete cats[dim];
  return { ...filters, cats };
}

// ---------------------------------------------------------------- counting

export type Tally = { counts: Float64Array; total: number; unit: Unit };
const effective = (dim: Dimension | null, unit: Unit): Unit => unit === 'features' && (!dim || dim.level !== 'feature') ? 'photos' : unit;
const unitKey = (ds: Dataset, unit: Unit, i: number) => unit === 'segments' ? ds.p.segment[i] : ds.p.pano[i] >= 0 ? ds.p.pano[i] : ds.snapshot.pano_count + i;

/** Counts per category in the chosen unit. `total` is distinct units in the selection (multi-valued categories overlap). */
export function tally(ds: Dataset, dim: Dimension, sel: Selection, unit: Unit): Tally {
  const u = effective(dim, unit), size = dim.categories.length, counts = new Float64Array(size), d = dim.data();
  if (u === 'features') {
    const feature = d.feature!;
    let total = 0;
    for (let f = 0; f < ds.fn; f++) if (sel.feature[f] && feature[f] >= 0) { counts[feature[f]]++; total++; }
    return { counts, total, unit: u };
  }
  const distinct = u !== 'photos';
  const seen = distinct ? new Set<number>() : null, seenAny = distinct ? new Set<number>() : null;
  let total = 0;
  for (let i = 0; i < ds.n; i++) {
    if (!sel.photo[i]) continue;
    const key = distinct ? unitKey(ds, u, i) : 0;
    if (distinct) { if (!seenAny!.has(key)) { seenAny!.add(key); total++; } } else total++;
    if (d.single) {
      const k = d.single[i];
      if (k < 0) continue;
      if (distinct) { const s = key * 64 + k; if (seen!.has(s)) continue; seen!.add(s); }
      counts[k]++;
    } else {
      const m = d.mask![i];
      for (let k = 0; k < size; k++) if (m & (1 << k)) {
        if (distinct) { const s = key * 64 + k; if (seen!.has(s)) continue; seen!.add(s); }
        counts[k]++;
      }
    }
  }
  return { counts, total, unit: u };
}
/** Denominator excluding structural categories (for shares among known values). */
export function knownTotal(dim: Dimension, t: Tally) {
  let total = 0;
  dim.categories.forEach((c, k) => { if (!c.structural) total += t.counts[k]; });
  return total;
}

export type Matrix = { rows: Dimension; cols: Dimension; unit: Unit; counts: Float64Array; rowTotals: Float64Array; colTotals: Float64Array; total: number; note: string };
export function crossTab(ds: Dataset, rows: Dimension, cols: Dimension, sel: Selection, unit: Unit): Matrix {
  const R = rows.categories.length, C = cols.categories.length, counts = new Float64Array(R * C);
  const rf = rows.data().feature, cf = cols.data().feature;
  let u: Unit = unit, note = '';
  if (unit === 'features') {
    if (!rf && !cf) { u = 'photos'; note = 'Neither attribute belongs to a feature, so photos are counted.'; }
    else if (rf && cf && rows.kind !== cols.kind && rows.kind !== -2 && cols.kind !== -2) { u = 'photos'; note = 'These attributes belong to different feature kinds, so co-occurrence is counted per photo.'; }
  }
  if (u === 'features') {
    const featureCode = (dim: Dimension, f: number, own?: Int8Array) => own ? (own[f] >= 0 ? [own[f]] : []) : photoCodes(dim, ds.fPhoto[f]);
    for (let f = 0; f < ds.fn; f++) {
      if (!sel.feature[f]) continue;
      const a = featureCode(rows, f, rf), b = featureCode(cols, f, cf);
      for (const r of a) for (const c of b) counts[r * C + c]++;
    }
    note ||= rf && cf ? 'Both attributes are read from the same feature.' : 'Each feature is paired with its photo’s attribute.';
  } else {
    const distinct = u !== 'photos', seen = new Set<number>();
    for (let i = 0; i < ds.n; i++) {
      if (!sel.photo[i]) continue;
      const a = photoCodes(rows, i), b = photoCodes(cols, i), key = distinct ? unitKey(ds, u, i) : 0;
      for (const r of a) for (const c of b) {
        if (distinct) { const s = (key * 64 + r) * 64 + c; if (seen.has(s)) continue; seen.add(s); }
        counts[r * C + c]++;
      }
    }
    note ||= rows.level === 'feature' || cols.level === 'feature' ? 'Multi-valued photos appear in every combination they show, so cells can overlap.' : '';
  }
  const rowTotals = new Float64Array(R), colTotals = new Float64Array(C);
  let total = 0;
  for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) { const v = counts[r * C + c]; rowTotals[r] += v; colTotals[c] += v; total += v; }
  return { rows, cols, unit: u, counts, rowTotals, colTotals, total, note };
}

/** Per-segment category counts (photo unit) for map/3D summaries. */
export function segmentComposition(ds: Dataset, dim: Dimension, sel: Selection) {
  const size = dim.categories.length, counts = new Float64Array(ds.segCount * size), photos = new Float64Array(ds.segCount);
  for (let i = 0; i < ds.n; i++) {
    if (!sel.photo[i]) continue;
    const s = ds.p.segment[i];
    photos[s]++;
    for (const k of photoCodes(dim, i)) counts[s * size + k]++;
  }
  return { counts, photos, size };
}

/** Grid cells (metres) over photo positions with per-category counts. */
export function gridCells(ds: Dataset, dim: Dimension, sel: Selection, metres: number) {
  const cells = new Map<string, { key: string; x: number; y: number; photos: number[]; counts: Float64Array }>();
  const size = dim.categories.length;
  for (let i = 0; i < ds.n; i++) {
    if (!sel.photo[i]) continue;
    const x = Math.floor((ds.p.lng[i] - 151.21) * 92300 / metres), y = Math.floor((ds.p.lat[i] + 33.88) * 111320 / metres);
    const key = `${x}:${y}`;
    let cell = cells.get(key);
    if (!cell) { cell = { key, x, y, photos: [], counts: new Float64Array(size) }; cells.set(key, cell); }
    cell.photos.push(i);
    for (const k of photoCodes(dim, i)) cell.counts[k]++;
  }
  return [...cells.values()].map((cell) => ({
    ...cell, west: 151.21 + cell.x * metres / 92300, east: 151.21 + (cell.x + 1) * metres / 92300,
    south: -33.88 + cell.y * metres / 111320, north: -33.88 + (cell.y + 1) * metres / 111320,
  }));
}

/** Histogram over a numeric column (months or minutes→days), split by a dimension's representative category. */
export function histogram(ds: Dataset, values: ArrayLike<number>, bucket: (v: number) => number, sel: Selection, dim: Dimension | null) {
  const buckets = new Map<number, Float64Array>();
  const size = dim ? dim.categories.length : 1;
  let missing = 0;
  for (let i = 0; i < ds.n; i++) {
    if (!sel.photo[i]) continue;
    const v = values[i];
    if (v < 0) { missing++; continue; }
    const b = bucket(v);
    let row = buckets.get(b);
    if (!row) { row = new Float64Array(size); buckets.set(b, row); }
    row[dim ? Math.max(0, photoCategory(dim, i)) : 0]++;
  }
  return { buckets, missing };
}

/** Tag-level attribute coverage: for each kind/tag, how many features have known, unknown or no value. */
export function attributeCoverage(ds: Dataset, sel: Selection) {
  const rows: { kind: string; tag: string; features: number; known: number; unknown: number; notRecorded: number; partial: number; confSum: number; confN: number }[] = [];
  const index = new Map<string, (typeof rows)[number]>();
  const kindFeatures = new Float64Array(ds.vocab.kinds.length);
  for (let f = 0; f < ds.fn; f++) if (sel.feature[f]) kindFeatures[ds.fKind[f]]++;
  ds.vocab.kinds.forEach((kind, k) => (ds.vocab.kind_tags[kind] ?? []).forEach((tag) => {
    const row = { kind, tag, features: kindFeatures[k], known: 0, unknown: 0, notRecorded: 0, partial: 0, confSum: 0, confN: 0 };
    rows.push(row); index.set(`${k}:${ds.tagIndex.get(tag)}`, row);
  }));
  const unknownValue = ds.vocab.tag_names.map((name) => (ds.vocab.tag_values[name] ?? []).indexOf('unknown'));
  const clear = ds.vocab.visibility.indexOf('clear');
  for (let f = 0; f < ds.fn; f++) {
    if (!sel.feature[f]) continue;
    for (let t = ds.tagStart[f]; t < ds.tagStart[f + 1]; t++) {
      const row = index.get(`${ds.fKind[f]}:${ds.tName[t]}`);
      if (!row) continue;
      if (ds.tValue[t] === unknownValue[ds.tName[t]]) row.unknown++;
      else { row.known++; if (ds.tVis[t] !== clear) row.partial++; if (ds.tConf[t] !== 127) { row.confSum += ds.tConf[t]; row.confN++; } }
    }
  }
  for (const row of rows) row.notRecorded = Math.max(0, row.features - row.known - row.unknown);
  return rows;
}

// ---------------------------------------------------------------- export

const quote = (value: string | number) => `"${String(value).replaceAll('"', '""')}"`;
const csv = (rows: (string | number)[][]) => rows.map((row) => row.map(quote).join(',')).join('\r\n');

export function photosCsv(ds: Dataset, dims: Map<string, Dimension>, sel: Selection, extra: Dimension[] = []) {
  const p = ds.p, segs = ds.snapshot.segments;
  const show = (id: string, i: number) => { const d = dims.get(id); return d ? photoCodes(d, i).map((k) => d.categories[k].label).join('; ') : ''; };
  const extras = extra.filter((d) => !['annotation', 'footpath', 'imagery_age', 'review_confirmation'].includes(d.id));
  const rows: (string | number)[][] = [['photo_id', 'code', 'street', 'segment', 'side', 'status', 'footpath', 'longitude', 'latitude', 'position_source',
    'heading', 'imagery_month', 'imagery_date_status', 'imagery_age_at_capture', 'captured_at', 'annotated_at', 'annotation_model', 'review_confirmation',
    'review_notes', 'lowest_model_confidence', 'panorama_photo_count', ...extras.map((d) => d.label)]];
  for (let i = 0; i < ds.n; i++) {
    if (!sel.photo[i]) continue;
    rows.push([p.id[i], p.code[i], ds.snapshot.streets[p.street[i]] ?? '', segs.key[p.segment[i]] ?? '', ds.vocab.sides[p.side[i]] ?? '',
      show('annotation', i), show('footpath', i), p.lng[i], p.lat[i], p.position[i] ? 'planned' : 'camera', p.heading[i],
      p.imagery_month[i] >= 0 ? monthIso(p.imagery_month[i]) : '', show('imagery_date_status', i), show('imagery_age', i),
      p.captured[i] >= 0 ? minuteDate(p.captured[i]).toISOString() : '', p.tagged[i] >= 0 ? minuteDate(p.tagged[i]).toISOString() : '',
      ds.vocab.model[p.model[i]] ?? '', show('review_confirmation', i), p.review_reasons[i] >= 0 ? p.review_reasons[i] : '',
      ds.minConf[i] >= 0 ? ds.minConf[i] / 100 : '', ds.panoSize[i] || '', ...extras.map((d) => show(d.id, i))]);
  }
  return csv(rows);
}
export function featuresCsv(ds: Dataset, sel: Selection) {
  const names = ds.vocab.tag_names, allTags = [...new Set(Object.values(ds.vocab.kind_tags).flat())];
  const rows: (string | number)[][] = [['photo_id', 'code', 'street', 'feature_kind', ...allTags.flatMap((t) => [t, `${t}_visibility`, `${t}_confidence`])]];
  for (let f = 0; f < ds.fn; f++) {
    if (!sel.feature[f]) continue;
    const i = ds.fPhoto[f], tags = new Map<string, [string, string, string]>();
    for (let t = ds.tagStart[f]; t < ds.tagStart[f + 1]; t++) {
      const name = names[ds.tName[t]];
      tags.set(name, [ds.vocab.tag_values[name]?.[ds.tValue[t]] ?? '', ds.vocab.visibility[ds.tVis[t]] ?? '', ds.tConf[t] === 127 ? '' : String(ds.tConf[t] / 100)]);
    }
    rows.push([ds.p.id[i], ds.p.code[i], ds.snapshot.streets[ds.p.street[i]] ?? '', ds.vocab.kinds[ds.fKind[f]], ...allTags.flatMap((t) => tags.get(t) ?? ['not_recorded', '', ''])]);
  }
  return csv(rows);
}
export function photosGeoJson(ds: Dataset, dims: Map<string, Dimension>, sel: Selection, colour: Dimension | null) {
  const features = [];
  const label = (id: string, i: number) => { const d = dims.get(id); return d ? photoCodes(d, i).map((k) => d.categories[k].key) : []; };
  for (let i = 0; i < ds.n; i++) {
    if (!sel.photo[i]) continue;
    features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [ds.p.lng[i], ds.p.lat[i]] }, properties: {
      id: ds.p.id[i], code: ds.p.code[i], street: ds.snapshot.streets[ds.p.street[i]], position_source: ds.p.position[i] ? 'planned' : 'camera',
      status: label('annotation', i)[0], footpath: label('footpath', i)[0],
      imagery_month: ds.p.imagery_month[i] >= 0 ? monthIso(ds.p.imagery_month[i]) : null,
      ...(colour ? { [colour.id]: label(colour.id, i) } : {}),
    } });
  }
  return JSON.stringify({ type: 'FeatureCollection', features });
}
export function matrixCsv(m: Matrix) {
  return csv([[`${m.rows.label} \\ ${m.cols.label} (${m.unit})`, ...m.cols.categories.map((c) => c.label), 'Total'],
    ...m.rows.categories.map((r, i) => [r.label, ...m.cols.categories.map((_, j) => m.counts[i * m.cols.categories.length + j]), m.rowTotals[i]])]);
}
