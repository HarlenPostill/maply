import { z } from 'zod';

const count = z.number().int().nonnegative();
const tally = z.object({ observed: count, flagged: count, examples: z.array(z.string().uuid()) });
const counts = {
  planned: count, saved: count, tagged: count, review: count,
  metrics: z.object({ footpath: tally, quality: tally, passage: tally, surface: tally, slope: tally, crossing: tally, hazards: tally }),
  examples: z.array(z.string().uuid()),
};
const segmentSchema = z.object({
  key: z.string(), street: z.string(), side: z.enum(['left', 'right', 'none']),
  coordinates: z.array(z.tuple([z.number(), z.number()])).min(2), ...counts,
});
const signal = z.union([z.literal(-1), z.literal(0), z.literal(1)]);
const pointSchema = z.object({
  id: z.string().uuid(), key: z.string(), code: z.string(), street: z.string(),
  side: z.enum(['left', 'right', 'none']), coordinates: z.tuple([z.number(), z.number()]),
  heading: z.number(), position_source: z.enum(['camera', 'planned']),
  metrics: z.object({ footpath: signal, quality: signal, passage: signal, surface: signal, slope: signal, crossing: signal, hazards: signal }),
});
export type PhotoPoint = z.infer<typeof pointSchema>;
export const snapshotSchema = z.object({
  version: z.literal(1), generated_at: z.string().datetime({ offset: true }),
  job_id: z.string().uuid(), job_name: z.string(), segments: z.array(segmentSchema), points: z.array(pointSchema),
});
export type Snapshot = z.infer<typeof snapshotSchema>;
export type SegmentSide = z.infer<typeof segmentSchema>;
export type Counts = Pick<SegmentSide, keyof typeof counts>;
export type Segment = Omit<SegmentSide, 'side'>;
export type Metric = 'coverage' | keyof Counts['metrics'];
export type Side = 'all' | SegmentSide['side'];
export const METRICS: { key: Metric; name: string; short: string; explanation: string }[] = [
  { key: 'coverage', name: 'Tagging coverage', short: 'Tagged', explanation: 'Tagged photos ÷ all planned photos. Grey streets have no tags yet.' },
  { key: 'quality', name: 'Footpath quality', short: 'Quality concerns', explanation: 'Photos with visible footpath defects, rough surfaces or interruptions ÷ photos with assessed quality. Missing footpaths and unknown quality remain separate.' },
  { key: 'passage', name: 'Narrowed or blocked passage', short: 'Passage concerns', explanation: 'Photos showing narrowed or blocked passage ÷ photos with a known passage observation.' },
  { key: 'surface', name: 'Surface concerns', short: 'Surface concerns', explanation: 'Photos showing a defect, rough texture, or loose/uneven surface type ÷ photos with known surface observations.' },
  { key: 'slope', name: 'Apparent slope', short: 'Slope concerns', explanation: 'Photos showing moderate or steep slopes ÷ photos with known slope observations. Slopes are visual estimates, not measurements.' },
  { key: 'crossing', name: 'Crossing transitions', short: 'Crossing concerns', explanation: 'Photos showing a missing ramp, unmodified kerb, steps or uneven ramp join ÷ photos with known crossing observations.' },
  { key: 'footpath', name: 'Missing footpath', short: 'Missing footpath', explanation: 'Photos labelled footpath absent ÷ photos with a known footpath observation.' },
  { key: 'hazards', name: 'Route hazards', short: 'Hazard observations', explanation: 'Tagged photos with an observed hazard on or encroaching into the route. No recorded hazard does not establish absence.' },
];
export function metricInfo(metric: Metric) { return METRICS.find((m) => m.key === metric) ?? METRICS[0]; }
export function emptyCounts(): Counts {
  const tally = () => ({ observed: 0, flagged: 0, examples: [] });
  return { planned: 0, saved: 0, tagged: 0, review: 0, examples: [], metrics: {
    footpath: tally(), quality: tally(), passage: tally(), surface: tally(), slope: tally(), crossing: tally(), hazards: tally(),
  } };
}
export function addCounts(target: Counts, row: Counts) {
  target.planned += row.planned; target.saved += row.saved; target.tagged += row.tagged; target.review += row.review;
  target.examples = [...new Set([...target.examples, ...row.examples])].slice(0, 12);
  for (const key of ['footpath', 'quality', 'passage', 'surface', 'slope', 'crossing', 'hazards'] satisfies (keyof Counts['metrics'])[]) {
    target.metrics[key].observed += row.metrics[key].observed;
    target.metrics[key].flagged += row.metrics[key].flagged;
    target.metrics[key].examples = [...new Set([...target.metrics[key].examples, ...row.metrics[key].examples])].slice(0, 12);
  }
}
export function groupSegments(rows: SegmentSide[], side: Side, street: string): Segment[] {
  const groups = new Map<string, Segment>();
  for (const row of rows) {
    if (side !== 'all' && row.side !== side || street && row.street !== street) continue;
    let group = groups.get(row.key);
    if (!group) {
      group = { key: row.key, street: row.street, coordinates: row.coordinates, ...emptyCounts() };
      groups.set(row.key, group);
    }
    addCounts(group, row);
  }
  return [...groups.values()];
}
export function total(rows: Counts[]): Counts { const result = emptyCounts(); rows.forEach((row) => addCounts(result, row)); return result; }
export function measure(row: Counts, metric: Metric) {
  const numerator = metric === 'coverage' ? row.tagged : row.metrics[metric].flagged;
  const denominator = metric === 'coverage' ? row.planned : metric === 'hazards' ? row.tagged : row.metrics[metric].observed;
  return { numerator, denominator, rate: denominator ? numerator / denominator : null };
}
export function colour(row: Counts, metric: Metric): string {
  const { rate } = measure(row, metric);
  if (rate === null || metric === 'coverage' && !row.tagged) return '#a7b2b8';
  if (metric === 'coverage') return rate >= .8 ? '#126e68' : rate >= .4 ? '#399a98' : '#a0c8c0';
  return rate === 0 ? '#4f969b' : rate < .25 ? '#e2bd61' : rate < .5 ? '#db884f' : '#b94749';
}
export function percent(rate: number | null) { return rate === null ? '—' : rate > 0 && rate < .01 ? '<1%' : rate > .99 && rate < 1 ? '>99%' : `${Math.round(rate * 100)}%`; }
export function downloadCsv(rows: Segment[], metric: Metric) {
  const quote = (value: string | number) => `"${String(value).replaceAll('"', '""')}"`;
  const csv = [['Street', 'Segment', 'Metric', 'Planned photos', 'Tagged photos', 'Signal photos', 'Denominator photos'],
    ...rows.map((row) => { const m = measure(row, metric); return [row.street, row.key, metric, row.planned, row.tagged, m.numerator, m.denominator]; })]
    .map((row) => row.map(quote).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = `maply-${metric}.csv`; link.click(); URL.revokeObjectURL(url);
}

export function pointSignal(point: PhotoPoint, metric: Metric): -1 | 0 | 1 {
  return metric === 'coverage' ? 1 : metric === 'hazards' ? point.metrics.hazards === 1 ? 1 : 0 : point.metrics[metric];
}
export function pointColour(point: PhotoPoint, metric: Metric): string {
  if (metric === 'quality' && point.metrics.footpath === 1) return '#72549a';
  const signal = pointSignal(point, metric);
  return signal === -1 ? '#a7b2b8' : signal === 1 && metric !== 'coverage' ? '#b94749' : '#25857b';
}
export function pointDescription(point: PhotoPoint, metric: Metric): string {
  if (metric === 'coverage') return 'Tagged photo';
  if (metric === 'quality') return point.metrics.footpath === 1 ? 'No footpath' : point.metrics.quality === 1 ? 'Visible quality concern' : point.metrics.quality === 0 ? 'No visible defect reported' : 'Quality unknown';
  if (metric === 'footpath') return point.metrics.footpath === 1 ? 'No footpath' : point.metrics.footpath === 0 ? 'Footpath present' : 'Presence unknown';
  return pointSignal(point, metric) === 1 ? 'Concern observed' : pointSignal(point, metric) === 0 ? 'No concern recorded' : 'Unknown / unobserved';
}
export function pointLegend(metric: Metric): [string, string][] {
  if (metric === 'quality') return [['#b94749', 'Quality concern'], ['#25857b', 'No visible defect reported'], ['#72549a', 'No footpath'], ['#a7b2b8', 'Unknown quality']];
  if (metric === 'footpath') return [['#b94749', 'No footpath'], ['#25857b', 'Footpath present'], ['#a7b2b8', 'Unknown presence']];
  if (metric === 'coverage') return [['#25857b', 'Tagged photo']];
  return [['#b94749', 'Concern observed'], ['#25857b', 'No concern recorded'], ['#a7b2b8', 'Unknown / unobserved']];
}
export function heatCells(points: PhotoPoint[], metric: Metric, size: number) {
  const cells = new Map<string, { key: string; west: number; south: number; east: number; north: number; observed: number; flagged: number; points: PhotoPoint[] }>();
  for (const point of points) {
    const x = Math.floor((point.coordinates[0] - 151.21) * 92300 / size);
    const y = Math.floor((point.coordinates[1] + 33.88) * 111320 / size);
    const key = `${x}:${y}`;
    let cell = cells.get(key);
    if (!cell) {
      cell = { key, west: 151.21 + x * size / 92300, east: 151.21 + (x + 1) * size / 92300,
        south: -33.88 + y * size / 111320, north: -33.88 + (y + 1) * size / 111320, observed: 0, flagged: 0, points: [] };
      cells.set(key, cell);
    }
    const value = pointSignal(point, metric);
    cell.observed += Number(value !== -1); cell.flagged += Number(value === 1); cell.points.push(point);
  }
  return [...cells.values()];
}
