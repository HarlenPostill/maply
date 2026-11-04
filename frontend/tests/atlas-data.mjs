import { test } from 'node:test';
import assert from 'node:assert/strict';
import { heatCells, pointSignal } from '../src/visualiser/data.ts';
const photo = (id, quality, footpath = 0) => ({
  id, key: 'one-segment', code: id, street: 'Example', side: 'left', coordinates: [151.21, -33.88],
  heading: 0, position_source: 'camera', metrics: { quality, footpath, passage: -1, surface: -1, slope: -1, crossing: -1, hazards: -1 },
});
test('a heatmap keeps coincident photos and excludes unknown and missing quality from the denominator', () => {
  const points = [photo('a', 1), photo('b', 0), photo('c', -1), photo('d', -1, 1)];
  const cells = heatCells(points, 'quality', 100);
  assert.equal(cells.length, 1);
  assert.equal(cells[0].points.length, 4);
  assert.equal(cells[0].observed, 2);
  assert.equal(cells[0].flagged, 1);
  const missing = heatCells(points, 'footpath', 100)[0];
  assert.equal(missing.observed, 4);
  assert.equal(missing.flagged, 1);
});
test('changing cell size preserves all counts and original coordinates', () => {
  const points = [photo('a', 1), { ...photo('b', 0), coordinates: [151.212, -33.882] }];
  const before = structuredClone(points);
  for (const size of [50, 100, 200]) {
    const cells = heatCells(points, 'quality', size);
    assert.equal(cells.reduce((n, c) => n + c.observed, 0), 2);
    assert.equal(cells.reduce((n, c) => n + c.flagged, 0), 1);
  }
  assert.deepEqual(points, before);
});
test('hazard frequency uses all tagged photos without claiming hazard absence', () => {
  const point = photo('a', -1);
  assert.equal(pointSignal(point, 'hazards'), 0);
  assert.equal(pointSignal(point, 'quality'), -1);
});
