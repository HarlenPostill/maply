import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  applyFilters, attributeCoverage, buildDataset, buildDimensions, crossTab, emptyFilters, featuresCsv, gridCells,
  parseSnapshot, percent, photoCategory, photosCsv, tally, toggleCategory,
} from '../src/visualiser/model.ts';

// Mirrors app.accessibility.pack_tag.
const VIS = ['clear', 'partial', 'blurred', 'occluded', 'outside_frame'];
const TAGS = { surface: ['concrete', 'asphalt', 'brick_pavers', 'unknown'], condition: ['no_visible_defect', 'visible_defect', 'unknown'], presence: ['present', 'absent', 'unknown'], type: ['bin', 'tree', 'unknown'], persistence: ['fixed', 'temporary', 'unknown'] };
const NAMES = Object.keys(TAGS);
const pack = (name, value, visibility = 'clear', confidence = .9) =>
  ((NAMES.indexOf(name) * 32 + TAGS[name].indexOf(value)) * 8 + VIS.indexOf(visibility)) * 128 + (confidence === null ? 127 : Math.round(confidence * 100));

function snapshot(photos, features) {
  const col = (key, fallback) => photos.map((p) => p[key] ?? fallback);
  return {
    version: 2, generated_at: '2026-11-12T00:00:00+00:00', digest: 'abc', job_id: 'job', job_name: 'Fixture', job_slug: 'fixture',
    vocabulary: {
      kinds: ['footpath_section', 'driveway', 'kerb_ramp', 'crossing', 'hazard'], visibility: VIS, sides: ['left', 'right', 'none'],
      annotation: ['none', 'current', 'stale'], presence: ['present', 'absent', 'unknown'], metrics: ['footpath', 'quality', 'passage', 'surface', 'slope', 'crossing', 'hazards'],
      tag_names: NAMES, tag_values: TAGS,
      kind_tags: { footpath_section: ['surface', 'condition', 'presence'], driveway: [], kerb_ramp: [], crossing: [], hazard: ['type', 'persistence'] },
      status: ['captured', 'accepted', 'needs_check', 'pending', 'no_imagery', 'blocked', 'skipped', 'retake'], imagery_date_type: ['available', 'not_available'],
      capture_source: ['automation'], model: ['m'], review_confirmation: ['automatic', 'human'], image_source: [], highway: ['residential'], osm_sidewalk: ['both'],
    },
    streets: ['A St', 'B St'],
    segments: { key: ['s0', 's1'], street: [0, 1], length_m: [100, 50], highway: [0, -1], osm_sidewalk: [0, -1], coordinates: [[[151.2, -33.9], [151.201, -33.9]], [[151.21, -33.91], [151.211, -33.91]]] },
    photos: {
      id: photos.map((_, i) => `p${i}`), code: photos.map((_, i) => `P${i}`), segment: col('segment', 0), street: col('street', 0), side: col('side', 0),
      status: col('status', 0), annotation: col('annotation', 1), lng: col('lng', 151.2), lat: col('lat', -33.9), position: col('position', 0),
      heading: col('heading', 90), pano: col('pano', -1), imagery_month: col('month', -1), imagery_date_type: col('dateType', -1), captured: col('captured', 29000000),
      capture_source: col('source', 0), tagged: col('tagged', 29000100), model: col('model', 0), review_confirmation: col('review', -1), image_source: col('imageSource', -1),
      review_reasons: col('reasons', 0), footpath: col('footpath', 0), footpath_visibility: col('fpVis', 0), signals: col('signals', 0),
    },
    features: { photo: features.map((f) => f[0]), kind: features.map((f) => f[1]), tags: features.map((f) => f[2]) },
    pano_count: Math.max(0, ...photos.map((p) => (p.pano ?? -1) + 1)),
  };
}

function fixture() {
  const photos = [
    { pano: 0, month: 2021 * 12 + 2 },                    // 0: concrete, no defect + bin hazard (fixed)
    { pano: 0, month: 2021 * 12 + 2 },                    // 1: same panorama, concrete, condition not recorded
    { month: 2024 * 12, segment: 1, street: 1 },          // 2: asphalt + brick pavers sections, unknown condition
    { footpath: 1 },                                       // 3: footpath absent, no features, undated
    { annotation: 0, status: 4, footpath: -1, signals: -1, captured: -1, tagged: -1, model: -1 }, // 4: no imagery
  ];
  const features = [
    [0, 0, [pack('presence', 'present'), pack('surface', 'concrete'), pack('condition', 'no_visible_defect')]],
    [0, 4, [pack('type', 'bin'), pack('persistence', 'fixed')]],
    [1, 0, [pack('presence', 'present'), pack('surface', 'concrete', 'partial', .72)]],
    [2, 0, [pack('presence', 'present'), pack('surface', 'asphalt'), pack('condition', 'unknown', 'occluded', null)]],
    [2, 0, [pack('presence', 'present'), pack('surface', 'brick_pavers')]],
    [2, 4, [pack('type', 'tree'), pack('persistence', 'temporary')]],
  ];
  const ds = buildDataset(parseSnapshot(snapshot(photos, features)));
  const dims = buildDimensions(ds);
  return { ds, dims, map: new Map(dims.map((d) => [d.id, d])) };
}
const counts = (dim, t) => Object.fromEntries(dim.categories.map((c, k) => [c.key, t.counts[k]]).filter(([, v]) => v));

test('incomplete columns are rejected', () => {
  const bad = snapshot([{}], []);
  bad.photos.lat = [];
  assert.throws(() => parseSnapshot(bad), /incomplete/);
});

test('footpath quality separates defects, unassessed quality, absence and missing annotation', () => {
  const ds = buildDataset(parseSnapshot(snapshot([
    { signals: 1 << 2 }, { signals: 2 << 2 }, { signals: 0 },
    { footpath: 1, signals: 0 }, { annotation: 0, signals: -1 },
  ], [])));
  const dims = buildDimensions(ds);
  const map = new Map(dims.map((d) => [d.id, d]));
  const dim = map.get('footpath_quality');
  const all = applyFilters(ds, map, emptyFilters());
  assert.deepEqual(counts(dim, tally(ds, dim, all, 'photos')), {
    no_visible_defect: 1, concern: 1, not_assessed: 1, no_footpath: 1, not_annotated: 1,
  });
  assert.equal(applyFilters(ds, map, toggleCategory(emptyFilters(), dim.id, 'concern')).photos, 1);
});

test('not recorded, unknown, missing kind and not annotated stay distinct', () => {
  const { ds, map } = fixture();
  const dim = map.get('tag:footpath_section:condition');
  const all = applyFilters(ds, map, emptyFilters());
  assert.deepEqual(counts(dim, tally(ds, dim, all, 'photos')), { no_visible_defect: 1, unknown: 1, not_recorded: 2, no_footpath_section: 1, not_annotated: 1 });
  // In feature units only real footpath sections count, so structural photo states disappear.
  assert.deepEqual(counts(dim, tally(ds, dim, all, 'features')), { no_visible_defect: 1, unknown: 1, not_recorded: 2 });
});

test('material is multi-valued per photo and features count individually', () => {
  const { ds, map } = fixture();
  const dim = map.get('tag:footpath_section:surface');
  const all = applyFilters(ds, map, emptyFilters());
  const photos = tally(ds, dim, all, 'photos');
  assert.equal(photos.total, 5);
  assert.deepEqual(counts(dim, photos), { concrete: 2, asphalt: 1, brick_pavers: 1, no_footpath_section: 1, not_annotated: 1 });
  assert.equal(tally(ds, dim, all, 'features').total, 4);
  // Shared panorama: photos 0 and 1 collapse to one panorama.
  assert.equal(tally(ds, dim, all, 'panoramas').counts[dim.categories.findIndex((c) => c.key === 'concrete')], 1);
  // Colouring picks the rarer of a photo's values; asphalt and brick pavers tie, so vocabulary order decides.
  assert.equal(dim.categories[photoCategory(dim, 2)].key, 'asphalt');
  assert.equal(dim.categories[photoCategory(dim, 0)].key, 'concrete');
});

test('feature filters apply per feature for the same kind, per photo otherwise', () => {
  const { ds, map } = fixture();
  const f = toggleCategory(emptyFilters(), 'tag:hazard:type', 'bin');
  const sel = applyFilters(ds, map, f);
  assert.equal(sel.photos, 1);
  const persistence = map.get('tag:hazard:persistence');
  assert.deepEqual(counts(persistence, tally(ds, persistence, sel, 'features')), { fixed: 1 });
  // The footpath section of the same photo still passes (different kind).
  assert.equal(sel.features, 2);
  const both = applyFilters(ds, map, toggleCategory(f, 'tag:footpath_section:surface', 'asphalt'));
  assert.equal(both.photos, 0);
});

test('dates are separate dimensions and undated imagery is not filled in', () => {
  const { ds, map } = fixture();
  const all = applyFilters(ds, map, emptyFilters());
  const year = map.get('imagery_year');
  assert.deepEqual(counts(year, tally(ds, year, all, 'photos')), { 2021: 2, 2024: 1, undated: 1, not_saved: 1 });
  const age = map.get('imagery_age');
  const ages = counts(age, tally(ds, age, all, 'photos'));
  assert.equal(ages.undated, 1);
  assert.equal(ages.no_capture_time, 1);
  const ranged = applyFilters(ds, map, { cats: {}, imagery: [2024 * 12, 2024 * 12 + 11] });
  assert.equal(ranged.photos, 1);
});

test('cross tabulation in features reads both attributes from the same feature', () => {
  const { ds, map } = fixture();
  const all = applyFilters(ds, map, emptyFilters());
  const m = crossTab(ds, map.get('tag:hazard:type'), map.get('tag:hazard:persistence'), all, 'features');
  const rows = map.get('tag:hazard:type').categories, cols = map.get('tag:hazard:persistence').categories;
  const cell = (r, c) => m.counts[rows.findIndex((x) => x.key === r) * cols.length + cols.findIndex((x) => x.key === c)];
  assert.equal(cell('bin', 'fixed'), 1);
  assert.equal(cell('bin', 'temporary'), 0);
  assert.equal(cell('tree', 'temporary'), 1);
  const mixed = crossTab(ds, map.get('tag:hazard:type'), map.get('tag:footpath_section:surface'), all, 'features');
  assert.equal(mixed.unit, 'photos', 'different feature kinds fall back to photo co-occurrence');
});

test('attribute coverage and exports honour the selection', () => {
  const { ds, map } = fixture();
  const all = applyFilters(ds, map, emptyFilters());
  const condition = attributeCoverage(ds, all).find((r) => r.kind === 'footpath_section' && r.tag === 'condition');
  assert.deepEqual([condition.features, condition.known, condition.unknown, condition.notRecorded], [4, 1, 1, 2]);
  const sel = applyFilters(ds, map, toggleCategory(emptyFilters(), 'footpath', 'absent'));
  const csv = photosCsv(ds, map, sel).split('\r\n');
  assert.equal(csv.length, 2);
  assert.ok(csv[1].startsWith('"p3","P3"'));
  assert.equal(featuresCsv(ds, sel).split('\r\n').length, 1);
  assert.equal(gridCells(ds, map.get('footpath'), all, 100).reduce((n, c) => n + c.photos.length, 0), 5);
});

test('percent never rounds small or near-total shares to misleading values', () => {
  assert.equal(percent(0.001), '<1%');
  assert.equal(percent(0.996), '>99%');
  assert.equal(percent(0), '0%');
  assert.equal(percent(0.0004, 1), '<0.1%');
  assert.equal(percent(null), '—');
});
