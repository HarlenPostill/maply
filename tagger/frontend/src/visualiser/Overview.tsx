import { useMemo } from 'react';
import type { Atlas } from './context';
import { BarList, Card, Columns, StackBar, Swatch, unitFor } from './charts';
import { fmt, knownTotal, METRIC_INFO, percent, tally, type Dimension, type Unit } from './model';

export function DimCard({ atlas, id, title, unit, limit = 8, className = '', note }: { atlas: Atlas; id: string; title?: string; unit?: Unit; limit?: number; className?: string; note?: string }) {
  const dim = atlas.dimMap.get(id);
  const u = unit ?? atlas.unit;
  const t = useMemo(() => dim ? tally(atlas.ds, dim, atlas.sel, u) : null, [atlas.ds, dim, atlas.sel, u]);
  const base = useMemo(() => dim && atlas.sel !== atlas.base ? tally(atlas.ds, dim, atlas.base, u) : undefined, [atlas.ds, dim, atlas.base, atlas.sel, u]);
  if (!dim || !t) return null;
  const known = knownTotal(dim, t);
  return <Card className={className} title={title ?? dim.label}
    subtitle={<>{fmt(known)} of {fmt(t.total)} {unitFor(dim, t.unit)} with a known value{note ? ` · ${note}` : ''}</>}
    actions={dim.colorable ? <button type="button" className="viz-text-button" onClick={() => { atlas.setColorBy(dim.id); atlas.goTo('map'); }}>Map it →</button> : undefined}>
    <BarList dim={dim} tally={t} baseline={base} limit={limit} selected={atlas.filters.cats[dim.id]} onToggle={(key, only) => atlas.toggle(dim.id, key, only)} />
  </Card>;
}

function Hero({ label, value, note }: { label: string; value: string; note: string }) {
  return <div className="viz-hero-tile"><span>{label}</span><strong>{value}</strong><small>{note}</small></div>;
}

export default function Overview({ atlas }: { atlas: Atlas }) {
  const { ds, sel, dimMap } = atlas;
  const stats = useMemo(() => {
    const annotation = dimMap.get('annotation')!, a = tally(ds, annotation, sel, 'photos');
    const panoramas = tally(ds, annotation, sel, 'panoramas').total;
    const streets = new Set<number>(), segments = new Set<number>();
    let dated = 0, saved = 0;
    for (let i = 0; i < ds.n; i++) if (sel.photo[i]) {
      streets.add(ds.p.street[i]); segments.add(ds.p.segment[i]);
      if (ds.p.imagery_month[i] >= 0) dated++;
      if (ds.p.annotation[i] === 1 || ['captured', 'accepted', 'needs_check'].includes(ds.vocab.status[ds.p.status[i]] ?? '')) saved++;
    }
    let metres = 0;
    for (const s of segments) metres += ds.snapshot.segments.length_m[s] ?? 0;
    return { annotation, a, panoramas, streets: streets.size, segments: segments.size, metres, dated, saved };
  }, [ds, sel, dimMap]);
  const year = dimMap.get('imagery_year')!;
  const yearTally = useMemo(() => tally(ds, year, sel, atlas.unit === 'features' ? 'photos' : atlas.unit), [ds, year, sel, atlas.unit]);
  const yearDomain = year.categories.map((_, k) => k).filter((k) => !year.categories[k].structural);
  const yearValues = useMemo(() => new Map(yearDomain.map((k) => [k, Float64Array.of(yearTally.counts[k])])), [yearTally, yearDomain.join()]);
  const metrics = ds.vocab.metrics.filter((m) => m !== 'footpath').map((m) => dimMap.get(`metric:${m}`)).filter((d): d is Dimension => !!d);
  const current = stats.a.counts[0];
  return <div className="viz-overview">
    <div className="viz-hero">
      <Hero label="Planned photos" value={fmt(sel.photos)} note={sel.photos === ds.n ? `${fmt(stats.streets)} streets · ${fmt(stats.segments)} segments` : `of ${fmt(ds.n)} · ${fmt(stats.streets)} streets`} />
      <Hero label="Annotated photos" value={fmt(current)} note={`${percent(sel.photos ? current / sel.photos : null)} of planned · current image only`} />
      <Hero label="Unique panoramas" value={fmt(stats.panoramas)} note="Overlapping photos from one panorama count once" />
      <Hero label="Annotated features" value={fmt(sel.features)} note="Footpath sections, driveways, ramps, crossings, hazards" />
      <Hero label="Planned street length" value={`${fmt(Math.round(stats.metres / 100) / 10)} km`} note="Segment centrelines with a matching photo" />
      <Hero label="Dated imagery" value={percent(stats.saved ? stats.dated / stats.saved : null)} note={`${fmt(stats.saved - stats.dated)} saved photos undated`} />
    </div>
    <Card title="From plan to annotation" subtitle="Every planned photo, by whether a current image annotation exists and why not.">
      <StackBar label="Photo pipeline" height={22} parts={stats.annotation.categories.map((c, k) => ({ key: c.key, label: c.label, value: stats.a.counts[k], color: c.color }))} />
      <div className="viz-inline-legend">{stats.annotation.categories.map((c, k) => stats.a.counts[k] > 0 && <button type="button" key={c.key} aria-pressed={!!atlas.filters.cats.annotation?.includes(c.key)} onClick={(e) => atlas.toggle('annotation', c.key, !e.shiftKey)}><Swatch color={c.color} />{c.label} <b>{fmt(stats.a.counts[k])}</b></button>)}</div>
    </Card>
    <div className="viz-card-grid">
      <DimCard atlas={atlas} id="footpath" unit={atlas.unit === 'features' ? 'photos' : undefined} />
      <DimCard atlas={atlas} id="tag:footpath_section:surface" title="Footpath surface material" note="material, not condition" />
      <DimCard atlas={atlas} id="tag:footpath_section:path_type" title="Footpath type" />
      <DimCard atlas={atlas} id="tag:footpath_section:continuity" title="Footpath continuity" />
      <DimCard atlas={atlas} id="feature_kind" />
      <DimCard atlas={atlas} id="tag:hazard:type" title="Hazard types" limit={10} />
      <DimCard atlas={atlas} id="tag:driveway:footpath_treatment" title="Footpath at driveways" />
      <DimCard atlas={atlas} id="tag:kerb_ramp:tactile_indicators" title="Tactile indicators at kerb ramps" />
      <DimCard atlas={atlas} id="tag:footpath_section:condition" title="Footpath condition (where recorded)" note="most sections were never assessed" />
    </div>
    <div className="viz-card-grid is-wide">
      <Card title="Imagery year" subtitle={<>Provider imagery month, by year · {fmt(yearTally.counts[year.categories.findIndex((c) => c.key === 'undated')])} undated {atlas.unit === 'features' ? 'photos' : atlas.unit} kept separate</>}
        actions={<button type="button" className="viz-text-button" onClick={() => atlas.goTo('time')}>Explore dates →</button>}>
        <Columns domain={yearDomain} values={yearValues} series={[{ key: 'n', label: 'Photos', color: '#2a78d6' }]} label="Imagery year histogram" height={190}
          tickLabel={(k) => year.categories[k].label} tooltip={(k) => `Imagery from ${year.categories[k].label}`} />
      </Card>
      <DimCard atlas={atlas} id="imagery_age" unit={atlas.unit === 'features' ? 'photos' : undefined} />
    </div>
    <div className="viz-card-grid">
      <DimCard atlas={atlas} id="review_confirmation" unit={atlas.unit === 'features' ? 'photos' : undefined} />
      <DimCard atlas={atlas} id="min_confidence" unit={atlas.unit === 'features' ? 'photos' : undefined} />
      <DimCard atlas={atlas} id="review_notes" unit={atlas.unit === 'features' ? 'photos' : undefined} />
    </div>
    <Card title="Derived concern indicators" subtitle="Transparent rules over existing tags. “Not assessed” is never treated as “no concern”. Shares use assessed photos only."
      actions={<button type="button" className="viz-text-button" onClick={() => atlas.goTo('coverage')}>Rules & coverage →</button>}>
      <div className="viz-metric-rows">{metrics.map((d) => {
        const t = tally(ds, d, sel, atlas.unit === 'features' ? 'photos' : atlas.unit);
        const assessed = t.counts[0] + t.counts[1];
        return <div key={d.id} className="viz-metric-row">
          <button type="button" className="viz-text-button" title={METRIC_INFO[d.id.slice(7)]?.rule} onClick={() => { atlas.setColorBy(d.id); atlas.goTo('map'); }}>{d.label}</button>
          <StackBar label={d.label} parts={d.categories.map((c, k) => ({ key: c.key, label: c.label, value: t.counts[k], color: c.color }))} />
          <span><b>{fmt(t.counts[0])}</b> concern / {fmt(assessed)} assessed{assessed ? ` (${percent(t.counts[0] / assessed)})` : ''} · {fmt(t.counts[2])} not assessed</span>
        </div>;
      })}</div>
      <div className="viz-inline-legend is-static">{metrics[0]?.categories.map((c) => <span key={c.key}><Swatch color={c.color} />{c.label}</span>)}</div>
    </Card>
  </div>;
}
