import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { api, type JobSummary, type Shot, type Status } from '../api';
import ObservationMap from './ObservationMap';
import PhotoMap from './PhotoMap';
import Insights from './Insights';
import { addCounts, colour, downloadCsv, emptyCounts, groupSegments, measure, METRICS, metricInfo, percent, snapshotSchema, total, pointSignal, type PhotoPoint, type Counts, type Metric, type Segment, type Side, type Snapshot } from './data';
import './visualiser.css';
const Observation3D = lazy(() => import('./Observation3D'));
type View = 'map' | 'streets' | '3d' | 'insights' | 'heatmap';
const number = (n: number) => n.toLocaleString();
const words = (s: string) => s.replaceAll('_', ' ');

export default function Visualiser({ jobs, status, onBack }: { jobs: JobSummary[]; status: Status; onBack: () => void }) {
  const [jobId, setJobId] = useState(jobs[0]?.id ?? '');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [view, setView] = useState<View>('map');
  const [metric, setMetric] = useState<Metric>('coverage');
  const [side, setSide] = useState<Side>('all');
  const [street, setStreet] = useState('');
  const [selectedPhoto, setSelectedPhoto] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [fit, setFit] = useState(0);
  const [focused, setFocused] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
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
    const load = async () => {
      if (pending || document.hidden) return;
      pending = true; setLoading(true);
      const requestController = new AbortController();
      controller = requestController;
      const timeout = window.setTimeout(() => requestController.abort(), 20000);
      try {
        const response = await fetch(`/assets/accessibility/${jobId}.json`, { cache: 'no-store', signal: requestController.signal });
        if (!response.ok) throw new Error('The analysis snapshot is not available yet. Refresh will try again.');
        const parsed = snapshotSchema.safeParse(await response.json());
        if (!parsed.success || parsed.data.job_id !== jobId) throw new Error('The analysis snapshot could not be read. Your last successful view is preserved.');
        if (!disposed) { setSnapshot(parsed.data); setError(''); setNow(Date.now()); }
      } catch (e) { if (!disposed) setError(e instanceof Error ? e.message : 'Could not refresh the analysis.'); }
      finally { clearTimeout(timeout); pending = false; if (!disposed) setLoading(false); }
    };
    void load();
    const timer = live ? window.setInterval(() => { void load(); }, 30000) : undefined;
    const visible = () => { if (!document.hidden && live) void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => { disposed = true; controller?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [jobId, live, refresh]);
  const data = snapshot?.job_id === jobId ? snapshot : null;
  const streets = useMemo(() => [...new Set(data?.segments.map((row) => row.street) ?? [])].sort(), [data]);
  const rows = useMemo(() => groupSegments(data?.segments ?? [], side, street), [data, side, street]);
  const points = useMemo(() => (data?.points ?? []).filter((point) => (side === 'all' || point.side === side) && (!street || point.street === street)), [data, side, street]);
  const selectSegment = (key: string) => { setSelected(key); setSelectedPhoto(null); };
  const selectPhoto = (id: string) => { const point = points.find((p) => p.id === id); if (point) { setSelected(point.key); setSelectedPhoto(id); } };
  const counts = useMemo(() => total(rows), [rows]);
  const selectedRow = rows.find((row) => row.key === selected) ?? null;
  const info = metricInfo(metric);
  const value = measure(counts, metric);
  const age = data ? Math.max(0, (now - Date.parse(data.generated_at)) / 1000) : null;
  const stale = age !== null && age > 120;
  const fitKey = `${jobId}:${street}:${side}:${fit}`;
  const pickStreet = (name: string) => { setStreet(name); setSelected(null); setSelectedPhoto(null); setView('map'); };
  return <div className={`visualiser ${focused ? 'viz-focused' : ''} ${collapsed ? 'viz-collapsed' : ''}`}>
    <header className="viz-header">
      <div className="viz-brand"><button className="wordmark" onClick={onBack}>maply<span> / </span></button><div><strong>Accessibility atlas</strong><span>Explore the evidence in your streets</span></div></div>
      <div className="viz-header-actions"><button className="btn" onClick={onBack}>← Capture workspace</button><button className="btn" disabled={!data} onClick={() => downloadCsv(rows, metric)}>Export CSV</button></div>
    </header>
    <div className="viz-body">
      <aside className="viz-sidebar" id="atlas-sidebar" hidden={collapsed}>
        <div className="viz-section"><span className="viz-eyebrow">YOUR DATASET</span>
          <label className="field"><span>Capture job</span><select aria-label="Capture job" value={jobId} onChange={(e) => { setJobId(e.target.value); setStreet(''); setSelected(null); setSelectedPhoto(null); setSnapshot(null); }}>{jobs.map((job) => <option key={job.id} value={job.id}>{job.name}</option>)}</select></label>
          <div className={`viz-live ${stale || error ? 'is-stale' : ''}`}><span className="viz-live-dot" /><strong>{error ? 'Refresh unavailable' : stale ? 'Snapshot delayed' : live ? 'Auto-refresh on' : 'Auto-refresh paused'}</strong><button className="viz-text-button" onClick={() => setLive(!live)}>{live ? 'Pause' : 'Resume'}</button></div>
          <div className="viz-refresh-line"><span>{data ? `Snapshot ${new Date(data.generated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}${stale ? ` · ${Math.floor((age ?? 0) / 60)} min old` : ''}` : 'Waiting for first snapshot'}</span><button className="viz-text-button" disabled={loading} onClick={() => setRefresh((v) => v + 1)}>{loading ? 'Updating…' : 'Refresh'}</button></div>
          <p className="viz-fine">Checks every 30 seconds. Pausing this view does not pause capture.</p>
        </div>
        <div className="viz-section"><span className="viz-eyebrow">EXPLORE A LAYER</span><div className="viz-layers">{METRICS.map((item) => <button key={item.key} aria-pressed={metric === item.key} onClick={() => setMetric(item.key)}><span>{item.name}</span><span>{percent(measure(counts, item.key).rate)}</span></button>)}</div></div>
        <div className="viz-section"><span className="viz-eyebrow">NARROW THE VIEW</span>
          <label className="field"><span>Street</span><select aria-label="Street" value={street} onChange={(e) => { setStreet(e.target.value); setSelected(null); setSelectedPhoto(null); }}><option value="">All streets</option>{streets.map((name) => <option key={name}>{name}</option>)}</select></label>
          <label className="field"><span>Planned street side</span><select aria-label="Planned street side" value={side} onChange={(e) => { const v = e.target.value; if (v === 'all' || v === 'left' || v === 'right' || v === 'none') { setSide(v); setSelected(null); setSelectedPhoto(null); } }}><option value="all">Both sides + unspecified</option><option value="left">Left side</option><option value="right">Right side</option><option value="none">Unspecified side</option></select></label>
          <p className="viz-fine">Left and right follow the planned street direction. Map lines combine the selected sides.</p>
          {(street || side !== 'all') && <button className="viz-text-button" onClick={() => { setStreet(''); setSide('all'); setSelected(null); setSelectedPhoto(null); }}>Clear filters</button>}
        </div>
        <details className="viz-method"><summary>How to read this atlas</summary><p>Each measure counts photos, not unique hazards, footpath length or accessible routes. Overlapping images may show the same feature.</p><p>Only labels matching the current saved image are included. Unknown tags stay out of known-observation denominators. Features explicitly beside the route are excluded from concern measures.</p><p>Model confidence is not a probability of accessibility. Partial or blurred observations can be included. Open source photos to assess evidence and review reasons.</p><p>Photo views use camera positions, falling back to planned positions. Neither locates the exact depicted footpath feature. Imagery may predate capture. Photo columns show individual observations; segment summaries show rates or counts. Heights are not terrain or obstacle heights.</p></details>
      </aside>
      <main className="viz-main">
        <div className="viz-title-row"><div><span className="viz-eyebrow">{street || 'AREA OVERVIEW'}</span><h1>{info.name}</h1><p>{info.explanation}</p>{metric === 'quality' && <p className="viz-quality-coverage">{number(counts.metrics.quality.observed)} photos with assessed quality · {number(points.filter((p) => p.metrics.quality === -1 && p.metrics.footpath !== 1).length)} with unknown quality · {number(counts.metrics.footpath.flagged)} with no footpath</p>}</div><span className="viz-photo-badge">IMAGE OBSERVATIONS</span></div>
        {error && <div className="error" role="alert">{error} {data && 'Showing the last successful snapshot.'}</div>}
        <div className="viz-stats">
          <Stat title="Tagged photos" value={data ? number(counts.tagged) : '—'} note={`${number(counts.planned)} planned · ${percent(counts.planned ? counts.tagged / counts.planned : null)} covered`} />
          <Stat title="Saved photos" value={data ? number(counts.saved) : '—'} note={`${number(Math.max(0, counts.saved - counts.tagged))} awaiting current tags`} />
          <Stat title={metric === 'coverage' ? 'Street segments' : info.short} value={data ? number(metric === 'coverage' ? rows.filter((row) => row.tagged > 0).length : value.numerator) : '—'} note={metric === 'coverage' ? `${number(rows.length)} planned segments` : `of ${number(value.denominator)} ${metric === 'hazards' ? 'tagged' : 'observed'} photos`} />
          <Stat title="With review notes" value={data ? number(counts.review) : '—'} note="Tagged photos flagged for review" />
        </div>
        <div className="viz-workspace">
          {collapsed && <label className="viz-compact-layer">Layer <select aria-label="Active layer" value={metric} onChange={(e) => { const item = METRICS.find((m) => m.key === e.target.value); if (item) setMetric(item.key); }}>{METRICS.map((m) => <option key={m.key} value={m.key}>{m.name}</option>)}</select><span>{street || 'All streets'} · {live ? 'Auto-refresh on' : 'Refresh paused'}{stale ? ' · Snapshot delayed' : ''}</span></label>}
          <div className="viz-toolbar"><div className="seg" aria-label="Visualisation type">{([{ key: 'map', label: 'Map' }, { key: 'heatmap', label: 'Heatmap' }, { key: 'streets', label: 'Street comparison' }, { key: '3d', label: '3D landscape' }, { key: 'insights', label: 'Insights' }] satisfies { key: View; label: string }[]).map((item) => <button key={item.key} aria-pressed={view === item.key} onClick={() => setView(item.key)}>{item.label}</button>)}</div><div className="viz-toolbar-actions"><button className="btn" aria-expanded={!collapsed} aria-controls="atlas-sidebar" onClick={() => setCollapsed(!collapsed)}>{collapsed ? 'Show sidebar' : 'Hide sidebar'}</button><button className="btn" aria-pressed={focused} onClick={() => setFocused(!focused)}>{focused ? 'Exit focus' : 'Focus view'}</button><select aria-label="Inspect street segment" value={selected ?? ''} onChange={(e) => { setSelectedPhoto(null); setSelected(e.target.value || null); if (view === 'streets' || view === 'insights') setView('map'); }}><option value="">Inspect a segment…</option>{rows.filter((row) => row.tagged > 0).map((row) => <option key={row.key} value={row.key}>{row.street} · {row.key.slice(0, 8)} · {row.tagged} tagged</option>)}</select><button className="viz-text-button" onClick={() => setFit((v) => v + 1)}>Fit to selection</button></div></div>
          <div className={`viz-canvas-layout ${selectedRow && (view === 'map' || view === '3d' || view === 'heatmap') ? 'has-detail' : ''}`}>
            <div className="viz-canvas">
              {!data ? <div className="viz-empty"><h2>{loading ? 'Building your view…' : 'No analysis snapshot yet'}</h2><p>{jobs.length ? 'The read-only publisher will make current observations available here.' : 'Create a capture job to start exploring its observations.'}</p></div> : !rows.length ? <div className="viz-empty"><h2>No segments match these filters</h2><button className="btn" onClick={() => { setSide('all'); setStreet(''); }}>Clear filters</button></div> : view === 'heatmap' || view === 'map' && (metric === 'quality' || metric === 'footpath') ? <PhotoMap points={points} metric={metric} status={status} selected={selectedPhoto} onSelect={selectPhoto} fitKey={fitKey} heatmap={view === 'heatmap'} /> : view === 'map' ? <ObservationMap rows={rows} metric={metric} status={status} selected={selected} onSelect={selectSegment} fitKey={fitKey} /> : view === '3d' ? <Suspense fallback={<div className="viz-empty">Loading 3D renderer…</div>}><Observation3D points={points} selectedPhoto={selectedPhoto} onPhotoSelect={selectPhoto} rows={rows} metric={metric} selected={selected} onSelect={selectSegment} fitKey={fitKey} /></Suspense> : view === 'insights' ? <Insights rows={rows} metric={metric} onExplore={(name, layer) => { setStreet(name); setMetric(layer); setSelected(null); setSelectedPhoto(null); setView('map'); }} /> : <StreetComparison rows={rows} metric={metric} onStreet={pickStreet} />}
              {data && rows.length > 0 && view === 'map' && metric !== 'quality' && metric !== 'footpath' && <div className="viz-legend"><strong>{metric === 'coverage' ? 'Tagged / planned photos' : metric === 'hazards' ? 'Hazard photos / tagged photos' : 'Concern / known-observation photos'}</strong><div>{(metric === 'coverage' ? [['#a7b2b8', 'No tags'], ['#a0c8c0', '<40%'], ['#399a98', '40–79%'], ['#126e68', '80–100%']] : [['#a7b2b8', 'No observations'], ['#4f969b', '0%'], ['#e2bd61', '<25%'], ['#db884f', '25–49%'], ['#b94749', '50–100%']]).map(([color, label]) => <span key={label}><i style={{ background: color }} />{label}</span>)}</div><small>{metric === 'coverage' ? 'Select a street segment to inspect its photos.' : '0% means no recorded concern in these observations; it does not establish accessibility.'}</small></div>}
            </div>
            {selectedRow && (view === 'map' || view === '3d' || view === 'heatmap') && <SegmentDetail key={`${jobId}:${selectedRow.key}:${side}:${selectedPhoto ?? ''}`} points={points} initialPhoto={selectedPhoto} onPhotoSelect={selectPhoto} row={selectedRow} metric={metric} revision={data?.generated_at ?? ''} onClose={() => { setSelected(null); setSelectedPhoto(null); }} />}
          </div>
        </div>
        <footer className="viz-footer"><span>{number(rows.length)} segments in view · {side === 'all' ? 'All planned sides' : `${words(side)} side`}</span><span>Observed evidence, not a certified accessibility assessment.</span></footer>
      </main>
    </div>
  </div>;
}
function Stat({ title, value, note }: { title: string; value: string; note: string }) { return <div className="viz-stat"><span>{title}</span><strong>{value}</strong><small>{note}</small></div>; }

function StreetComparison({ rows, metric, onStreet }: { rows: Segment[]; metric: Metric; onStreet: (name: string) => void }) {
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('count');
  const streets = useMemo(() => {
    const groups = new Map<string, Counts>();
    rows.forEach((row) => { const group = groups.get(row.street) ?? emptyCounts(); addCounts(group, row); groups.set(row.street, group); });
    return [...groups].map(([street, counts]) => ({ street, counts, ...measure(counts, metric) }));
  }, [rows, metric]);
  const filtered = streets.filter((row) => row.street.toLowerCase().includes(search.toLowerCase())).sort((a, b) => sort === 'name' ? a.street.localeCompare(b.street) : sort === 'rate' ? (b.rate ?? -1) - (a.rate ?? -1) || b.denominator - a.denominator : b.numerator - a.numerator || b.denominator - a.denominator);
  return <div className="viz-comparison"><div className="viz-comparison-controls"><input aria-label="Search streets" placeholder="Find a street…" value={search} onChange={(e) => setSearch(e.target.value)} /><label>Sort <select value={sort} onChange={(e) => setSort(e.target.value)}><option value="count">Photo count</option><option value="rate">Percentage</option><option value="name">Street name</option></select></label></div>
    <p className="viz-fine">{filtered.length} streets. Percentages use photo observations; a small denominator can produce a high rate.</p>
    <table className="viz-table"><thead><tr><th>Street</th><th>{metricInfo(metric).short}</th><th>Photos</th><th>Tagging coverage</th></tr></thead><tbody>{filtered.map((row) => <tr key={row.street}><td><button className="viz-text-button" onClick={() => onStreet(row.street)}>{row.street}</button></td><td><div className="viz-bar"><span style={{ width: `${(row.rate ?? 0) * 100}%`, background: colour(row.counts, metric) }} /></div><b>{percent(row.rate)}</b></td><td>{number(row.numerator)} / {number(row.denominator)}</td><td>{number(row.counts.tagged)} / {number(row.counts.planned)}</td></tr>)}</tbody></table>
    {!filtered.length && <p className="viz-empty">No streets match your search.</p>}
  </div>;
}
function SegmentDetail({ row, metric, revision, points, initialPhoto, onPhotoSelect, onClose }: { row: Segment; metric: Metric; revision: string; points: PhotoPoint[]; initialPhoto: string | null; onPhotoSelect: (id: string) => void; onClose: () => void }) {
  const [photoId, setPhotoId] = useState(initialPhoto ?? '');
  const [photo, setPhoto] = useState<Shot | null>(null);
  const [error, setError] = useState('');
  const ids = points.filter((p) => p.key === row.key).sort((a, b) => Number(pointSignal(b, metric) === 1) - Number(pointSignal(a, metric) === 1) || a.code.localeCompare(b.code)).map((p) => p.id);
  const current = ids.includes(photoId) ? photoId : ids[0] ?? '';
  const location = points.find((p) => p.id === current);
  useEffect(() => {
    let disposed = false; setPhoto(null); setError('');
    if (current) api.get<Shot>(`/api/shots/${current}`).then((value) => { if (!disposed) setPhoto(value); }).catch((e: Error) => { if (!disposed) setError(e.message); });
    return () => { disposed = true; };
  }, [current, revision]);
  return <aside className="viz-detail"><div className="viz-detail-head"><span className="viz-eyebrow">SEGMENT EVIDENCE</span><button className="viz-text-button" onClick={onClose} aria-label="Close segment evidence">Close ×</button></div><h2>{row.street}</h2><p>{number(row.tagged)} / {number(row.planned)} photos tagged</p>
    <div className="viz-evidence-metrics">{METRICS.filter((m) => m.key !== 'coverage').map((item) => { const m = measure(row, item.key); return <div key={item.key}><span>{item.short}</span><strong>{m.numerator} / {m.denominator}</strong></div>; })}</div>
    <p className="viz-fine">All current tagged photos in this segment, with matching concerns first. Camera positions do not locate the exact depicted feature.</p>
    {!ids.length ? <p className="viz-empty">No current tagged photos in this segment yet.</p> : <><label className="field"><span>Source photo ({ids.length} photos)</span><select value={current} onChange={(e) => { setPhotoId(e.target.value); onPhotoSelect(e.target.value); }}>{ids.map((id, i) => <option key={id} value={id}>Photo {i + 1} · {points.find((p) => p.id === id)?.code ?? id.slice(0, 8)}</option>)}</select></label>
      {error && <div className="error">{error}</div>}{!photo && !error && <p>Loading evidence…</p>}
      {photo && <><div className="viz-photo-meta"><strong>{photo.code}</strong><span>{words(photo.side)} · {photo.footpath || 'side unspecified'}</span></div>{photo.image_url && <a href={photo.image_url} target="_blank" rel="noreferrer"><img className="viz-evidence-photo" src={photo.thumb_url || photo.image_url} alt={`${photo.street}, ${photo.side} side, source photo ${photo.code}`} /><span className="viz-text-button">Open full-size source ↗</span></a>}
        {location && <p className="viz-fine">{location.position_source === 'camera' ? 'Observed camera' : 'Planned position fallback'}: {location.coordinates[1].toFixed(6)}, {location.coordinates[0].toFixed(6)}. This is not the feature's surveyed position.</p>}
        <p className="viz-fine">Imagery date: {typeof photo.result.image_date === 'string' ? photo.result.image_date : 'not recorded'}. Captured: {photo.captured_at ? new Date(photo.captured_at).toLocaleDateString() : 'not recorded'}.</p>
        {photo.labels ? <><p><strong>Footpath:</strong> {words(photo.labels.footpath.value)}</p><p className="viz-fine">{photo.labels.footpath.evidence}</p>{photo.labels.features.map((feature) => <details className="viz-feature" key={feature.id}><summary>{words(feature.kind)} · {feature.location_description}</summary>{Object.entries(feature.tags).map(([name, tag]) => <div className="viz-tag" key={name}><strong>{words(name)}: {words(tag.value)}</strong><span>{tag.evidence}</span><small>{tag.visibility} · {tag.confidence === null ? 'Confidence unknown' : `${Math.round(tag.confidence * 100)}% model confidence`}</small></div>)}</details>)}{photo.labels.review_reasons.length > 0 && <details className="viz-feature"><summary>{photo.labels.review_reasons.length} review notes</summary><ul>{photo.labels.review_reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></details>}</> : <p>Tags have changed since this snapshot. Refresh for the latest evidence.</p>}
      </>}
    </>}
  </aside>;
}
