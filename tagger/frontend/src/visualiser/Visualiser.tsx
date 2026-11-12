import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { JobSummary, Status } from '../api';
import type { Atlas, Group, View } from './context';
import { BarList, DimensionSelect, Swatch, UNIT_WORD } from './charts';
import { applyFilters, buildDataset, buildDimensions, emptyFilters, featuresCsv, filterCount, fmt, metaSchema, minuteDate, monthLabel, parseSnapshot, photosCsv, photosGeoJson, tally, toggleCategory, UNITS, type Dataset, type Filters, type Snapshot, type Unit } from './model';
import { download } from './download';
import AtlasMap from './AtlasMap';
import Overview from './Overview';
import Compare from './Compare';
import Timeline from './Timeline';
import Coverage from './Coverage';
import Streets from './Streets';
import Gallery from './Gallery';
import Evidence from './Evidence';
import './visualiser.css';

const Observation3D = lazy(() => import('./Observation3D'));
const VIEWS: { key: View; label: string; hint: string }[] = [
  { key: 'overview', label: 'Overview', hint: 'At-a-glance composition of the dataset' },
  { key: 'map', label: 'Map', hint: 'Every photo, segment or grid cell coloured by any attribute' },
  { key: '3d', label: '3D', hint: 'Photo or segment columns in 3D' },
  { key: 'compare', label: 'Compare', hint: 'Cross-tabulate any two attributes' },
  { key: 'time', label: 'Time', hint: 'Imagery, capture and annotation dates' },
  { key: 'coverage', label: 'Coverage', hint: 'What was and was not assessed' },
  { key: 'streets', label: 'Streets', hint: 'Street-by-street profiles' },
  { key: 'photos', label: 'Photos', hint: 'Browse matching source photos' },
];
const DEFAULT_FACETS = ['footpath', 'tag:footpath_section:path_type', 'tag:footpath_section:surface', 'tag:footpath_section:continuity', 'feature_kind', 'tag:hazard:type', 'imagery_year', 'review_confirmation', 'side', 'highway'];
const ago = (seconds: number) => seconds < 90 ? `${Math.round(seconds)} s ago` : `${Math.round(seconds / 60)} min ago`;

function Facet({ atlas, id, onRemove }: { atlas: Atlas; id: string; onRemove: () => void }) {
  const dim = atlas.dimMap.get(id);
  // Facet counts ignore the facet's own filter so several values can be combined.
  const others = useMemo(() => {
    if (!atlas.filters.cats[id]) return atlas.sel;
    const cats = { ...atlas.filters.cats }; delete cats[id];
    return applyFilters(atlas.ds, atlas.dimMap, { ...atlas.filters, cats });
  }, [atlas.ds, atlas.dimMap, atlas.filters, atlas.sel, id]);
  const t = useMemo(() => dim ? tally(atlas.ds, dim, others, atlas.unit) : null, [atlas.ds, dim, others, atlas.unit]);
  if (!dim || !t) return null;
  const active = atlas.filters.cats[id]?.length ?? 0;
  return <details className="viz-facet" open={active > 0 || DEFAULT_FACETS.slice(0, 4).includes(id)}>
    <summary><span>{dim.label}</span>{active > 0 && <b>{active}</b>}<button type="button" className="viz-icon-button" aria-label={`Remove ${dim.label} facet`} onClick={(e) => { e.preventDefault(); onRemove(); }}>×</button></summary>
    <BarList dim={dim} tally={t} compact limit={6} selected={atlas.filters.cats[id]} onToggle={(key) => atlas.toggle(id, key)} />
  </details>;
}

export default function Visualiser({ jobs, status, onBack }: { jobs: JobSummary[]; status: Status; onBack: () => void }) {
  const [jobId, setJobId] = useState(jobs[0]?.id ?? '');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [view, setView] = useState<View>('overview');
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [unit, setUnit] = useState<Unit>('photos');
  const [colorId, setColorId] = useState('tag:footpath_section:surface');
  const [focus, setFocus] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [group, setGroup] = useState<Group | null>(null);
  const [facets, setFacets] = useState(DEFAULT_FACETS);
  const [streetQuery, setStreetQuery] = useState('');
  const [fit, setFit] = useState(0);
  const [focused, setFocused] = useState(false);
  const [collapsed, setCollapsed] = useState(() => typeof window !== 'undefined' && window.innerWidth < 760);
  const digest = useRef('');

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setFocused(false); };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);
  useEffect(() => { if (!jobId && jobs[0]) setJobId(jobs[0].id); }, [jobId, jobs]);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    if (!jobId) { setLoading(false); return; }
    let disposed = false, pending = false;
    let controller: AbortController | null = null;
    const load = async (force: boolean) => {
      if (pending || (document.hidden && !force)) return;
      pending = true; setLoading(true);
      const request = new AbortController(); controller = request;
      const timeout = window.setTimeout(() => request.abort(), 45000);
      try {
        // Poll the tiny manifest; download the full snapshot only when its content digest changes.
        const metaResponse = await fetch(`/assets/accessibility/${jobId}.meta.json`, { cache: 'no-store', signal: request.signal });
        const meta = metaResponse.ok ? metaSchema.safeParse(await metaResponse.json()) : null;
        if (meta?.success && meta.data.digest === digest.current && !force) { if (!disposed) { setCheckedAt(meta.data.checked_at); setError(''); } return; }
        const response = await fetch(`/assets/accessibility/${jobId}.json`, { cache: 'no-store', signal: request.signal });
        if (!response.ok) throw new Error('The analysis snapshot is not available yet. Refresh will try again.');
        let parsed: Snapshot;
        try { parsed = parseSnapshot(await response.json()); } catch { throw new Error('The analysis snapshot could not be read. Your last successful view is preserved.'); }
        if (parsed.job_id !== jobId) throw new Error('The analysis snapshot belongs to another job.');
        if (!disposed) { digest.current = parsed.digest; setSnapshot(parsed); setCheckedAt(meta?.success ? meta.data.checked_at : parsed.generated_at); setError(''); setNow(Date.now()); }
      } catch (e) { if (!disposed && !(e instanceof DOMException && e.name === 'AbortError' && disposed)) setError(e instanceof Error ? e.message : 'Could not refresh the analysis.'); }
      finally { clearTimeout(timeout); pending = false; if (!disposed) setLoading(false); }
    };
    void load(refresh > 0);
    const timer = live ? window.setInterval(() => { void load(false); }, 30000) : undefined;
    const visible = () => { if (!document.hidden && live) void load(false); };
    document.addEventListener('visibilitychange', visible);
    return () => { disposed = true; controller?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [jobId, live, refresh]);

  const data = snapshot?.job_id === jobId ? snapshot : null;
  const ds = useMemo<Dataset | null>(() => data ? buildDataset(data) : null, [data]);
  const dims = useMemo(() => ds ? buildDimensions(ds) : [], [ds]);
  const dimMap = useMemo(() => new Map(dims.map((d) => [d.id, d])), [dims]);
  const sel = useMemo(() => ds ? applyFilters(ds, dimMap, filters) : null, [ds, dimMap, filters]);
  const base = useMemo(() => ds ? applyFilters(ds, dimMap, emptyFilters()) : null, [ds, dimMap]);
  const colorBy = dimMap.get(colorId) ?? dims.find((d) => d.colorable) ?? null;
  const selectedPhoto = useMemo(() => { if (!ds || !selectedId) return null; const i = ds.p.id.indexOf(selectedId); return i >= 0 ? i : null; }, [ds, selectedId]);
  const toggle = useCallback((dim: string, key: string, only = false) => { setFilters((f) => toggleCategory(f, dim, key, only)); setGroup(null); }, []);
  const setFiltersAndReset = useCallback((f: Filters) => { setFilters(f); setGroup(null); }, []);
  const fitKey = `${jobId}:${(filters.cats.street ?? []).join('|')}:${filters.segment ?? ''}:${fit}`;
  const atlas: Atlas | null = ds && sel && base && colorBy ? {
    ds, dims, dimMap, sel, base, filters, unit, setFilters: setFiltersAndReset, toggle, colorBy,
    setColorBy: (id) => { setColorId(id); setFocus(null); }, focus, setFocus,
    selectedPhoto, selectPhoto: (i) => setSelectedId(i === null ? null : ds.p.id[i]), group, selectGroup: (g) => { setGroup(g); setSelectedId(null); },
    status, fitKey, goTo: setView,
  } : null;

  const checked = checkedAt ? Math.max(0, (now - Date.parse(checkedAt)) / 1000) : null;
  const stale = checked !== null && checked > 120;
  const count = filterCount(filters);
  const streets = data?.streets ?? [];
  const addStreet = (name: string) => { if (streets.includes(name)) { toggle('street', name); setStreetQuery(''); } };
  const exportName = `maply-${data?.job_slug || 'atlas'}${count ? '-filtered' : ''}`;
  const chips = atlas ? [
    ...Object.entries(filters.cats).map(([id, keys]) => {
      const d = dimMap.get(id);
      return { key: id, label: `${d?.label ?? id}: ${keys.map((k) => d?.categories.find((c) => c.key === k)?.label ?? k).join(' or ')}`, clear: () => { const cats = { ...filters.cats }; delete cats[id]; setFiltersAndReset({ ...filters, cats }); } };
    }),
    ...(filters.imagery ? [{ key: 'imagery', label: `Imagery ${monthLabel(filters.imagery[0])} – ${monthLabel(filters.imagery[1])}`, clear: () => setFiltersAndReset({ ...filters, imagery: undefined }) }] : []),
    ...(filters.captured ? [{ key: 'captured', label: `Captured ${minuteDate(filters.captured[0]).toLocaleDateString('en-AU', { timeZone: 'UTC' })} – ${minuteDate(filters.captured[1]).toLocaleDateString('en-AU', { timeZone: 'UTC' })}`, clear: () => setFiltersAndReset({ ...filters, captured: undefined }) }] : []),
    ...(filters.tagged ? [{ key: 'tagged', label: `Annotated ${minuteDate(filters.tagged[0]).toLocaleDateString('en-AU', { timeZone: 'UTC' })} – ${minuteDate(filters.tagged[1]).toLocaleDateString('en-AU', { timeZone: 'UTC' })}`, clear: () => setFiltersAndReset({ ...filters, tagged: undefined }) }] : []),
  ] : [];
  const showDetail = !!atlas && (selectedPhoto !== null || !!group);
  const viewBody = !atlas ? null : view === 'overview' ? <Overview atlas={atlas} /> : view === 'map' ? <AtlasMap atlas={atlas} />
    : view === '3d' ? <Suspense fallback={<div className="viz-empty">Loading 3D renderer…</div>}><Observation3D atlas={atlas} /></Suspense>
      : view === 'compare' ? <Compare atlas={atlas} /> : view === 'time' ? <Timeline atlas={atlas} /> : view === 'coverage' ? <Coverage atlas={atlas} />
        : view === 'streets' ? <Streets atlas={atlas} /> : <Gallery atlas={atlas} />;
  const canvasView = view === 'map' || view === '3d';
  return <div className={`visualiser ${focused ? 'viz-focused' : ''} ${collapsed ? 'viz-collapsed' : ''}`}>
    <header className="viz-header">
      <div className="viz-brand"><button className="wordmark" onClick={onBack}>maply<span> / </span></button><div><strong>Accessibility atlas</strong><span>{data ? data.job_name : 'Explore the evidence in your streets'}</span></div></div>
      <div className="viz-header-actions">
        {jobs.length > 1 && <select className="viz-job" aria-label="Capture job" value={jobId} onChange={(e) => { setJobId(e.target.value); setFilters(emptyFilters()); setSelectedId(null); setGroup(null); setSnapshot(null); digest.current = ''; }}>{jobs.map((job) => <option key={job.id} value={job.id}>{job.name}</option>)}</select>}
        <div className={`viz-live ${stale || error ? 'is-stale' : ''}`} title={data ? `Data generated ${new Date(data.generated_at).toLocaleString()}` : undefined}>
          <span className="viz-live-dot" /><span>{error ? 'Refresh unavailable' : stale ? 'Snapshot delayed' : live ? 'Live' : 'Paused'}{checked !== null ? ` · checked ${ago(checked)}` : ''}</span>
          <button type="button" className="viz-text-button" onClick={() => setLive(!live)}>{live ? 'Pause' : 'Resume'}</button>
          <button type="button" className="viz-text-button" disabled={loading} onClick={() => setRefresh((v) => v + 1)}>{loading ? 'Updating…' : 'Refresh'}</button>
        </div>
        <details className="viz-export"><summary className="btn">Export</summary><div>
          <p className="viz-fine">{atlas ? `${fmt(atlas.sel.photos)} photos · ${fmt(atlas.sel.features)} features${count ? ' (filtered)' : ''}` : 'No data yet'}</p>
          <button type="button" className="btn" disabled={!atlas} onClick={() => atlas && download(`${exportName}-photos.csv`, photosCsv(atlas.ds, dimMap, atlas.sel, [atlas.colorBy]), 'text/csv')}>Photos (CSV)</button>
          <button type="button" className="btn" disabled={!atlas} onClick={() => atlas && download(`${exportName}-features.csv`, featuresCsv(atlas.ds, atlas.sel), 'text/csv')}>Features & tags (CSV)</button>
          <button type="button" className="btn" disabled={!atlas} onClick={() => atlas && download(`${exportName}-photos.geojson`, photosGeoJson(atlas.ds, dimMap, atlas.sel, atlas.colorBy), 'application/geo+json')}>Photo points (GeoJSON)</button>
          <small>Exports honour every active filter. Feature exports apply filters on the same feature kind per feature.</small>
        </div></details>
        <button className="btn" onClick={onBack}>← Capture workspace</button>
      </div>
    </header>
    <div className="viz-body">
      <aside className="viz-sidebar" id="atlas-sidebar" hidden={collapsed} aria-label="Filters">
        <div className="viz-section">
          <span className="viz-eyebrow">FILTER THE DATASET</span>
          <form className="viz-street-search" onSubmit={(e) => { e.preventDefault(); addStreet(streetQuery); }}>
            <input list="viz-streets" type="search" aria-label="Filter by street" placeholder="Add a street…" value={streetQuery} onChange={(e) => { setStreetQuery(e.target.value); if (streets.includes(e.target.value)) addStreet(e.target.value); }} />
            <datalist id="viz-streets">{streets.map((s) => <option key={s} value={s} />)}</datalist>
          </form>
          <p className="viz-fine">Values combine with OR within a facet and AND across facets. Counts are in {UNIT_WORD[unit][1]}{unit === 'features' ? ' for feature attributes, photos otherwise' : ''}.</p>
        </div>
        {atlas && facets.map((id) => <Facet key={id} atlas={atlas} id={id} onRemove={() => setFacets(facets.filter((f) => f !== id))} />)}
        {atlas && <div className="viz-section"><label className="field"><span>Add a facet</span>
          <DimensionSelect dims={dims} filter={(d) => !facets.includes(d.id) && d.id !== 'street'} placeholder="Choose an attribute…" value="" onChange={(id) => id && setFacets([...facets, id])} label="Add a facet" /></label></div>}
        <details className="viz-method"><summary>How to read this atlas</summary>
          <p>Everything here is drawn from saved image annotations matched to the exact current image. Nothing is re-tagged or inferred. Unknown, not recorded and missing values are kept separate from known values and from “no concern”.</p>
          <p>Counting units differ: photos, unique panoramas (removing duplicate overlapping imagery), segments, or individual features. Photos with several feature values appear under each, so category counts can add up to more than the total.</p>
          <p>Materials such as concrete describe surface type, not quality. Condition was recorded on only a small share of footpath sections, and the schema has no severity grades.</p>
          <p>Model confidence is self-reported and uncalibrated. Photo positions are camera positions, not surveyed features. Imagery months come from the provider and may predate capture.</p>
        </details>
      </aside>
      <main className="viz-main">
        <div className="viz-toolbar">
          <nav className="viz-tabs" aria-label="Atlas views">{VIEWS.map((v) => <button key={v.key} type="button" aria-pressed={view === v.key} title={v.hint} onClick={() => setView(v.key)}>{v.label}</button>)}</nav>
          <div className="viz-toolbar-actions">
            <div className="seg" role="group" aria-label="Counting unit">{UNITS.map((u) => <button key={u.key} type="button" aria-pressed={unit === u.key} title={u.note} onClick={() => setUnit(u.key)}>{u.label}</button>)}</div>
            {atlas && <label className="viz-colour">Colour by <DimensionSelect dims={dims} value={atlas.colorBy.id} onChange={(id) => atlas.setColorBy(id)} label="Colour by" filter={(d) => d.colorable} /></label>}
            <button className="btn" aria-expanded={!collapsed} aria-controls="atlas-sidebar" onClick={() => setCollapsed(!collapsed)}>{collapsed ? 'Show filters' : 'Hide filters'}</button>
            {canvasView && <button className="btn" onClick={() => setFit((v) => v + 1)}>Fit</button>}
            <button className="btn" aria-pressed={focused} onClick={() => setFocused(!focused)}>{focused ? 'Exit focus' : 'Focus view'}</button>
          </div>
        </div>
        <div className="viz-filterbar" aria-live="polite">
          {atlas ? <><span className="viz-result"><b>{fmt(atlas.sel.photos)}</b> of {fmt(atlas.ds.n)} photos · <b>{fmt(atlas.sel.features)}</b> features</span>
            {chips.map((chip) => <span className="viz-chip" key={chip.key}>{chip.label}<button type="button" aria-label={`Remove filter ${chip.label}`} onClick={chip.clear}>×</button></span>)}
            {count > 0 && <button type="button" className="viz-text-button" onClick={() => setFiltersAndReset(emptyFilters())}>Clear all filters</button>}
            {!count && <span className="viz-fine">No filters. Click any bar, legend entry or matrix cell to filter; shift-click to add values.</span>}
            <span className="viz-chip is-colour"><Swatch color={atlas.colorBy.categories.find((c) => !c.structural)?.color ?? '#999'} />{atlas.colorBy.label}</span></>
            : <span className="viz-fine">{loading ? 'Loading the dataset snapshot…' : 'Waiting for the first snapshot'}</span>}
        </div>
        {error && <div className="error" role="alert">{error} {data && 'Showing the last successful snapshot.'}</div>}
        <div className={`viz-canvas-layout ${showDetail ? 'has-detail' : ''} ${canvasView ? 'is-canvas' : 'is-scroll'}`}>
          <div className="viz-canvas">
            {!atlas ? <div className="viz-empty"><h2>{loading ? 'Building your view…' : 'No analysis snapshot yet'}</h2><p>{jobs.length ? 'The read-only publisher writes current observations here every 30 seconds.' : 'Create a capture job to start exploring its observations.'}</p></div>
              : atlas.sel.photos === 0 ? <div className="viz-empty"><h2>No photos match these filters</h2><p>Remove a filter to see data again. Empty results are never shown as zero-concern areas.</p><button className="btn" onClick={() => setFiltersAndReset(emptyFilters())}>Clear filters</button></div>
                : viewBody}
          </div>
          {showDetail && atlas && <Evidence atlas={atlas} onClose={() => { setSelectedId(null); setGroup(null); }} />}
        </div>
        <footer className="viz-footer"><span>{data ? `Snapshot ${new Date(data.generated_at).toLocaleString('en-AU')} · ${fmt(data.photos.id.length)} planned photos · ${fmt(data.features.kind.length)} features` : ''}</span><span>Observed evidence from imagery, not a certified accessibility assessment.</span></footer>
      </main>
    </div>
  </div>;
}
