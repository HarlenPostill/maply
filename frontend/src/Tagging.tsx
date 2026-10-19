import { useEffect, useState } from 'react';
import { api, type AccessibilityFeature, type FC, type Shot, type ShotPage, type TagCounts, type TaggingSummary, type TagSelector, type VisionTag } from './api';
import { TAG_COLORS } from './MapView';
import { ConfidenceCard, confidencePercent, imageConfidence } from './Confidence';
import { featureStyles } from './ImageRegions';

export const words = (value: string) => value.replaceAll('_', ' ');
export const tagStatus = (shot: Shot) => shot.tagging?.status ?? 'unlabelled';

export function aggregateSegments(data: FC): FC {
  const groups = new Map<string, FC['features'][number]>();
  const keys: (keyof TagCounts)[] = ['eligible', 'unlabelled', 'queued', 'running', 'completed', 'failed', 'needs_review', 'cancelled'];
  for (const feature of data.features) {
    const p = feature.properties;
    let existing = groups.get(p.segment_key);
    if (!existing) {
      existing = { ...feature, properties: { ...p, ...Object.fromEntries(keys.map((k) => [k, 0])) } };
      groups.set(p.segment_key, existing);
    }
    for (const key of keys) existing.properties[key] += Number(p[key] ?? 0);
  }
  for (const f of groups.values()) {
    const p = f.properties;
    p.status = !p.eligible ? 'unavailable' : p.running ? 'running' : p.queued ? 'queued' : p.failed ? 'failed' : p.completed === p.eligible ? 'completed' : p.completed ? 'partial' : 'unlabelled';
  }
  return { type: 'FeatureCollection', features: [...groups.values()] };
}

export function TagLegend() {
  return <div className="tag-legend" aria-label="Tagging map legend">
    <strong>Image tagging</strong>
    {Object.entries(TAG_COLORS).map(([key, color]) => <span key={key}><i style={{ background: color }} />{key === 'unavailable' ? 'No saved images' : words(key)}</span>)}
    <small>Completion describes image analysis, not accessibility.</small>
  </div>;
}

export function TaggingPanel({ jobId, summary, error, loading, busy, selectedSegment, segments, onSelectSegment, onQueue, onCancel, onPhoto, onRefresh, onFocusMap, activity }: {
  jobId: string; summary: TaggingSummary | null; error: string; loading: boolean; busy: boolean;
  selectedSegment: string | null; segments: FC | null;
  onSelectSegment: (key: string | null) => void;
  onQueue: (selector: TagSelector) => void;
  onCancel: (selector: TagSelector) => void;
  onPhoto: (shot: Shot) => void;
  onRefresh: () => void;
  onFocusMap: (selector: TagSelector) => void;
  activity?: React.ReactNode;
}) {
  const [query, setQuery] = useState('');
  const [streets, setStreets] = useState<string[]>([]);
  const [side, setSide] = useState<'both' | 'left' | 'right' | 'none'>('both');
  const [photos, setPhotos] = useState<ShotPage>({ total: 0, items: [] });
  const [photoError, setPhotoError] = useState('');
  const [photoLoading, setPhotoLoading] = useState(false);
  const [photoLimit, setPhotoLimit] = useState(60);
  useEffect(() => { setPhotoLimit(60); }, [selectedSegment, side]);
  const segment = segments?.features.find((f) => f.properties.segment_key === selectedSegment);
  const selectedCounts = summary?.streets.filter((s) => streets.includes(s.street));
  const eligible = selectedSegment ? segment?.properties.eligible ?? 0 : selectedCounts?.reduce((n, s) => n + s.eligible, 0) ?? 0;
  const scope: TagSelector = { ...(selectedSegment ? { segment_keys: [selectedSegment] } : { streets }), ...(side === 'both' ? {} : { side }) };
  const hasSelection = !!selectedSegment || streets.length > 0;
  const version = JSON.stringify(summary?.counts);
  const visibleStreets = (summary?.streets ?? [])
    .filter((s) => s.street.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => (b.eligible - b.completed) - (a.eligible - a.completed)
      || Number(b.eligible > 0) - Number(a.eligible > 0)
      || a.street.localeCompare(b.street));

  useEffect(() => {
    if (!selectedSegment) { setPhotos({ total: 0, items: [] }); return; }
    let active = true;
    setPhotoLoading(true);
    setPhotoError('');
    api.get<ShotPage>(`/api/jobs/${jobId}/shots?include_labels=false&segment_key=${encodeURIComponent(selectedSegment)}${side === 'both' ? '' : `&side=${side}`}&status=captured,accepted,needs_check&limit=${photoLimit}`).then((p) => { if (active) setPhotos(p); }).catch((e) => { if (active) setPhotoError(e.message); }).finally(() => { if (active) setPhotoLoading(false); });
    return () => { active = false; };
  }, [jobId, selectedSegment, side, version, photoLimit]);

  return <section className="step tagging-panel" aria-label="Accessibility tagging">
    <div className="tag-heading"><h2>Accessibility tagging</h2><button className="btn btn-quiet" onClick={onRefresh} disabled={loading}>Refresh</button></div>
    <p className="hint">Analyse saved images for footpaths, surfaces, driveways and kerb transitions.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {!summary ? <p className="hint">{loading ? 'Loading tagging progress…' : 'Tagging progress unavailable.'}</p> : <>
      <div className="tag-progress">
        <div className="progress-line"><strong>{summary.counts.completed.toLocaleString()}</strong> / {summary.counts.eligible.toLocaleString()} images tagged</div>
        <span className="meter"><span style={{ width: `${summary.counts.eligible ? summary.counts.completed / summary.counts.eligible * 100 : 0}%` }} /></span>
        <div className="tag-counts"><span>{summary.counts.queued} queued</span><span>{summary.counts.running} running</span><span>{summary.counts.failed} failed</span><span>{summary.counts.needs_review} need review</span>{summary.counts.cancelled > 0 && <span>{summary.counts.cancelled} cancelled</span>}</div>
      </div>
      <div className={`worker-state ${summary.worker.online ? 'worker-online' : ''}`}>
        <strong><i />{summary.worker.online ? 'Vision worker connected' : 'Vision worker offline'}</strong>
        <span>{summary.worker.model || summary.model}</span>
        {!summary.worker.online && <><p>Queued images wait until the worker starts on your Mac.</p><code>cd backend &amp;&amp; uv run python ../scripts/tag_worker.py</code></>}
        {summary.worker.error && <p className="error">{summary.worker.error}</p>}
      </div>
      {activity}
      <ConfidenceCard confidence={summary.confidence} images={summary.counts.completed} eligible={summary.counts.eligible} aggregate />
      <div className="tag-scope">
        {selectedSegment ? <>
          <div className="tag-heading"><strong>Selected section</strong><button className="btn btn-quiet" onClick={() => onSelectSegment(null)}>Clear</button></div>
          <p>{segment?.properties.street || 'Unnamed street'}</p>
          <p className="fineprint">{segment?.properties.completed ?? 0} of {eligible} saved images tagged (all sides). Click another section on the map to change selection.</p>
        </> : <>
          <label className="field"><span>Choose streets, or click a section on the map</span><input type="search" aria-label="Find streets for tagging" placeholder="Find a street…" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
          <p className="fineprint">Most saved images awaiting tags first. Streets without saved images appear last.</p>
          <ul className="tag-streets checklist checklist-scroll">
            {visibleStreets.map((s) => <li key={s.street}>
              <label><input type="checkbox" checked={streets.includes(s.street)} onChange={(e) => setStreets((prev) => e.target.checked ? [...prev, s.street] : prev.filter((name) => name !== s.street))} /><span className="street-score-name">{s.street || 'Unnamed street'}<small>{s.eligible ? `${s.completed}/${s.eligible} images tagged` : 'No saved images'}{s.confidence && s.completed > 0 ? ` · ${s.confidence.known} known · ${s.confidence.unknown} unknown observations` : ''}</small></span><small className="street-confidence" title="Mean of tagged image scores; not measured accuracy">{confidencePercent(s.confidence?.mean)}</small></label>
            </li>)}
          </ul>
          <p className="fineprint">Street scores show mean model confidence for tagged images. Untagged images and unknown observations do not raise the score.</p>
          {streets.length > 0 && <div className="tag-heading"><span className="fineprint">{streets.length} selected · {eligible} saved images (all sides)</span><button className="btn btn-quiet" onClick={() => setStreets([])}>Clear</button></div>}
        </>}
        <label className="field"><span>Photographed side</span><select value={side} onChange={(e) => setSide(e.target.value as typeof side)}><option value="both">All sides</option><option value="left">Left</option><option value="right">Right</option><option value="none">No side / intersection</option></select></label>
        <button type="button" className="btn btn-wide" disabled={!hasSelection} onClick={() => onFocusMap(scope)}>Show selection on map</button>
        <button type="button" className="btn btn-primary btn-wide" disabled={busy || !hasSelection || !eligible} onClick={() => onQueue(scope)}>{busy ? 'Updating…' : selectedSegment ? 'Tag this section' : `Tag ${streets.length || 'selected'} street${streets.length === 1 ? '' : 's'}`}</button>
        <p className="fineprint">Only saved images on the chosen side are queued. Existing completed results are kept.</p>
        <div className="tag-secondary"><button type="button" className="btn" disabled={busy || !hasSelection} onClick={() => onQueue({ ...scope, retry: true })}>Retry failed / cancelled</button><button type="button" className="btn btn-quiet" disabled={busy || !hasSelection} onClick={() => onCancel(scope)}>Cancel selected work</button></div>
      </div>
      {selectedSegment && <div className="section-photos">
        <h3>Section images</h3>
        {photoError && <p className="error">{photoError}</p>}
        {photoLoading && photos.items.length === 0 ? <p className="hint">Loading images…</p> : photos.items.length === 0 ? <p className="hint">No saved images for this side.</p> : <ul>{photos.items.map((s) => <li key={s.id}><button className="section-photo" onClick={() => onPhoto(s)}>{s.thumb_url && <img src={s.thumb_url} alt="" loading="lazy" />}<span><strong>{s.code}</strong><small>{s.side} · {words(tagStatus(s))}{s.tagging?.needs_review ? ' · review' : ''}</small></span></button></li>)}</ul>}
        {photos.total > photos.items.length && <button className="btn" disabled={photoLoading} onClick={() => setPhotoLimit((n) => n + 60)}>{photoLoading ? 'Loading…' : `Show more (${photos.total - photos.items.length} left)`}</button>}
      </div>}
    </>}
  </section>;
}

function TagValue({ name, tag }: { name: string; tag: VisionTag }) {
  return <div className="vision-tag">
    <div className="vision-tag-title"><span>{words(name)}</span><span className={`confidence ${tag.confidence !== null && tag.confidence < .8 ? 'confidence-low' : ''}`} title="Model-reported confidence; not calibrated accuracy">{tag.confidence === null ? 'Unassessed' : `${Math.round(tag.confidence * 100)}%`}</span></div>
    <strong>{words(tag.value)}</strong>
    <p>{tag.evidence}</p>
    <small>Visibility: {words(tag.visibility)}</small>
  </div>;
}

export function TagResults({ shot, busy, onQueue, focusedFeature, onFocus }: { shot: Shot; busy: boolean; onQueue: () => void; focusedFeature: string | null; onFocus: (f: AccessibilityFeature) => void }) {
  const labels = shot.labels;
  const status = tagStatus(shot);
  return <section className="tag-results" aria-label="Image tagging results">
    <div className="tag-heading"><h3>Accessibility observations</h3><span className={`tag-badge tag-${status}`}>{words(status)}</span></div>
    {shot.tagging?.error && <p className="error" role="alert">{shot.tagging.error}</p>}
    {shot.image_url && ['captured', 'accepted', 'needs_check'].includes(shot.status) && status !== 'completed' && <button className="btn btn-primary" disabled={busy || status === 'queued' || status === 'running'} onClick={onQueue}>{status === 'queued' ? 'Waiting for vision worker…' : status === 'running' ? 'Analysing image…' : status === 'failed' || status === 'cancelled' ? 'Retry image tagging' : 'Tag this image'}</button>}
    {labels ? <>
      <ConfidenceCard confidence={shot.confidence ?? imageConfidence(labels)} />
      {labels.provenance?.imagery_date && <p className="fineprint">Imagery date: {labels.provenance.imagery_date}</p>}
      {shot.tagging?.model && <p className="fineprint">Model: {shot.tagging.model}</p>}
      {labels.review_reasons.length > 0 && <div className="review-reasons"><strong>Needs review</strong><ul>{labels.review_reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></div>}
      <TagValue name="Footpath presence" tag={labels.footpath} />
      {featureStyles(labels.features).map(({ feature: f, number, color, label }, index) => <details className={`vision-feature ${focusedFeature === f.id ? 'feature-focused' : ''}`} key={f.id} open={index === 0 ? true : undefined}>
        <summary><span className="feature-number" style={{ background: color }}>{number}</span>{label} <small>{f.id}</small></summary>
        <p className="feature-location">{f.location_description}</p>
        {f.region && <button className="btn btn-quiet" onClick={() => onFocus(f)}>{focusedFeature === f.id ? 'Region highlighted above' : 'Show region in image'}</button>}
        {Object.entries(f.tags).map(([name, tag]) => <TagValue key={name} name={name} tag={tag} />)}
      </details>)}
      {labels.notes.length > 0 && <ul className="vision-notes">{labels.notes.map((note, i) => <li key={i}>{note}</li>)}</ul>}
    </> : <p className="fineprint">Each observed feature receives its own tags, evidence and confidence. Unclear details remain unknown.</p>}
  </section>;
}
