import { useMemo, useState } from 'react';
import type { Atlas } from './context';
import { BarList, Card, Columns, DimensionSelect, Swatch } from './charts';
import { DAY, fmt, histogram, imageryAgeYears, minuteDate, monthLabel, tally, type Dimension } from './model';

const dayLabel = (day: number) => minuteDate(day * DAY).toLocaleDateString('en-AU', { day: 'numeric', month: 'short', timeZone: 'UTC' });

function Stacked({ atlas, values, bucket, domain, dim, label, tickLabel, tooltip, mode, range, onRange, height }: {
  atlas: Atlas; values: ArrayLike<number>; bucket: (v: number) => number; domain: number[]; dim: Dimension | null; label: string;
  tickLabel: (v: number) => string; tooltip: (v: number) => string; mode: 'count' | 'share';
  range?: [number, number] | null; onRange?: (r: [number, number] | null) => void; height?: number;
}) {
  const h = useMemo(() => histogram(atlas.ds, values, bucket, atlas.sel, dim), [atlas.ds, values, bucket, atlas.sel, dim]);
  const series = dim ? dim.categories.map((c) => ({ key: c.key, label: c.label, color: c.color })) : [{ key: 'n', label: 'Photos', color: '#2a78d6' }];
  return <><Columns domain={domain} values={h.buckets} series={series} label={label} tickLabel={tickLabel} tooltip={tooltip} mode={mode} range={range} onRange={onRange} height={height} />
    {h.missing > 0 && <p className="viz-fine">{fmt(h.missing)} selected photos have no value here and are not plotted.</p>}</>;
}

export default function Timeline({ atlas }: { atlas: Atlas }) {
  const { ds, dims, dimMap, filters } = atlas;
  const [split, setSplit] = useState<string>('');
  const [grain, setGrain] = useState<'month' | 'year'>('month');
  const [mode, setMode] = useState<'count' | 'share'>('count');
  const dim = split ? dimMap.get(split) ?? null : null;
  const range = ds.monthRange;
  const monthDomain = useMemo(() => {
    if (!range) return [];
    const out: number[] = [];
    if (grain === 'year') for (let y = Math.floor(range[0] / 12); y <= Math.floor(range[1] / 12); y++) out.push(y * 12);
    else for (let m = range[0]; m <= range[1]; m++) out.push(m);
    return out;
  }, [range, grain]);
  const monthBucket = useMemo(() => grain === 'year' ? (m: number) => Math.floor(m / 12) * 12 : (m: number) => m, [grain]);
  const ages = useMemo(() => {
    const out = new Array<number>(ds.n);
    for (let i = 0; i < ds.n; i++) out[i] = ds.p.imagery_month[i] >= 0 && ds.p.captured[i] >= 0 ? Math.max(0, imageryAgeYears(ds.p.imagery_month[i], ds.p.captured[i])) : -1;
    return out;
  }, [ds]);
  const dayDomain = (values: number[]) => {
    let lo = Infinity, hi = -Infinity;
    for (const v of values) if (v >= 0) { lo = Math.min(lo, Math.floor(v / DAY)); hi = Math.max(hi, Math.floor(v / DAY)); }
    return Number.isFinite(lo) ? Array.from({ length: hi - lo + 1 }, (_, k) => lo + k) : [];
  };
  const capturedDays = useMemo(() => dayDomain(ds.p.captured), [ds]);
  const taggedDays = useMemo(() => dayDomain(ds.p.tagged), [ds]);
  const day = (v: number) => Math.floor(v / DAY);
  const status = dimMap.get('imagery_date_status')!;
  const statusTally = useMemo(() => tally(ds, status, atlas.sel, 'photos'), [ds, status, atlas.sel]);
  const setRange = (key: 'imagery' | 'captured' | 'tagged', r: [number, number] | null) => atlas.setFilters({ ...filters, [key]: r ?? undefined });
  const imageryRange: [number, number] | null = filters.imagery ? [monthBucket(filters.imagery[0]), monthBucket(filters.imagery[1])] : null;
  const legend = dim && <div className="viz-inline-legend is-static">{dim.categories.map((c) => <span key={c.key}><Swatch color={c.color} hollow={c.structural} />{c.label}</span>)}</div>;
  return <div className="viz-timeline">
    <div className="viz-compare-controls">
      <label>Split by <DimensionSelect dims={dims} filter={(d) => d.colorable} placeholder="Nothing (totals)" value={split} onChange={setSplit} label="Split dates by" /></label>
      <div className="seg" role="group" aria-label="Bar values"><button type="button" aria-pressed={mode === 'count'} onClick={() => setMode('count')}>Counts</button><button type="button" aria-pressed={mode === 'share'} disabled={!dim} onClick={() => setMode('share')}>Share</button></div>
      <button type="button" className="viz-text-button" onClick={() => setSplit(atlas.colorBy.id)}>Use map colour ({atlas.colorBy.label})</button>
    </div>
    <p className="viz-fine">Three different clocks, never substituted: <b>imagery month</b> is when the provider says the Street View imagery was taken; <b>captured</b> is when the screenshot was saved; <b>annotated</b> is when the model tagged it. Counts are photos; a photo with several values is split by its rarest value.</p>
    <Card title="Imagery month" subtitle={<>When the depicted conditions were photographed. Drag to filter a range{filters.imagery ? <> · filtered to {monthLabel(filters.imagery[0])} – {monthLabel(filters.imagery[1])} <button type="button" className="viz-text-button" onClick={() => setRange('imagery', null)}>clear</button></> : ''}.</>}
      actions={<div className="seg" role="group" aria-label="Imagery grain"><button type="button" aria-pressed={grain === 'month'} onClick={() => setGrain('month')}>Months</button><button type="button" aria-pressed={grain === 'year'} onClick={() => setGrain('year')}>Years</button></div>}>
      <Stacked atlas={atlas} values={ds.p.imagery_month} bucket={monthBucket} domain={monthDomain} dim={dim} label="Imagery month histogram" mode={dim ? mode : 'count'}
        tickLabel={(m) => grain === 'year' || m % 12 === 0 ? String(Math.floor(m / 12)) : ''} tooltip={(m) => grain === 'year' ? `Imagery from ${Math.floor(m / 12)}` : `Imagery month ${monthLabel(m)}`}
        range={imageryRange} onRange={(r) => setRange('imagery', r && [r[0], grain === 'year' ? r[1] + 11 : r[1]])} height={240} />
      {legend}
    </Card>
    <div className="viz-card-grid is-wide">
      <Card title="Undated imagery" subtitle="Saved photos without a provider imagery month stay out of the timeline rather than being given another date.">
        <BarList dim={status} tally={statusTally} selected={filters.cats[status.id]} onToggle={(k, only) => atlas.toggle(status.id, k, only)} shareOf="total" />
      </Card>
      <Card title="Imagery age at capture" subtitle="Whole years between the imagery month (mid-month) and screenshot capture. Older imagery may not show current conditions.">
        <Stacked atlas={atlas} values={ages} bucket={Math.floor} domain={Array.from({ length: 20 }, (_, k) => k)} dim={dim} label="Imagery age histogram" mode={dim ? mode : 'count'}
          tickLabel={(y) => `${y}`} tooltip={(y) => `${y}–${y + 1} years old at capture`} height={190} />
      </Card>
    </div>
    <div className="viz-card-grid is-wide">
      <Card title="Captured (screenshot saved)" subtitle={<>Per UTC day. Drag to filter{filters.captured ? <> · <button type="button" className="viz-text-button" onClick={() => setRange('captured', null)}>clear capture range</button></> : ''}.</>}>
        <Stacked atlas={atlas} values={ds.p.captured} bucket={day} domain={capturedDays} dim={dim ?? dimMap.get('capture_source')!} label="Capture day histogram" mode={mode}
          tickLabel={dayLabel} tooltip={(d) => `Captured ${dayLabel(d)}`} range={filters.captured ? [day(filters.captured[0]), day(filters.captured[1])] : null}
          onRange={(r) => setRange('captured', r && [r[0] * DAY, r[1] * DAY + DAY - 1])} height={180} />
        {!dim && <div className="viz-inline-legend is-static">{dimMap.get('capture_source')!.categories.map((c) => <span key={c.key}><Swatch color={c.color} hollow={c.structural} />{c.label}</span>)}</div>}
      </Card>
      <Card title="Annotated (current tags)" subtitle={<>Per UTC day, from annotation provenance. Drag to filter{filters.tagged ? <> · <button type="button" className="viz-text-button" onClick={() => setRange('tagged', null)}>clear annotation range</button></> : ''}.</>}>
        <Stacked atlas={atlas} values={ds.p.tagged} bucket={day} domain={taggedDays} dim={dim ?? dimMap.get('model')!} label="Annotation day histogram" mode={mode}
          tickLabel={dayLabel} tooltip={(d) => `Annotated ${dayLabel(d)}`} range={filters.tagged ? [day(filters.tagged[0]), day(filters.tagged[1])] : null}
          onRange={(r) => setRange('tagged', r && [r[0] * DAY, r[1] * DAY + DAY - 1])} height={180} />
        {!dim && <div className="viz-inline-legend is-static">{dimMap.get('model')!.categories.map((c) => <span key={c.key}><Swatch color={c.color} hollow={c.structural} />{c.label}</span>)}</div>}
      </Card>
    </div>
  </div>;
}
