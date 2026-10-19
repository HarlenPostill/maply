import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, type Job, type Prompt, type Shot, type ShotPage, type Status, type FC, type TaggingSummary, type TagSelector, type AccessibilityFeature } from './api';
import type { Cone } from './MapView';
import { aggregateSegments, TaggingPanel, TagLegend, TagResults, tagStatus, words } from './Tagging';
import { ImageRegions } from './ImageRegions';
import { ActivityPeek, TaggingActivity, useTaggingActivity } from './TaggingActivity';
import { confidencePercent } from './Confidence';
import ParallelSessions, { useParallelSessions } from './ParallelSessions';
import type { MapState } from './App';
import { ago, bytes, flagWords, STATUS_WORDS } from './words';

type Props = {
  id: string;
  status: Status;
  setMap: (s: MapState) => void;
  setOverlay: (n: React.ReactNode) => void;
  onBack: () => void;
  onDeleted: () => void;
};

const MAP_MIN_ZOOM = 16; // below this, big jobs show no individual shots
const SMALL_JOB = 4000; // small jobs load every shot at once
const FILTERS = {
  saved: { label: 'Saved', status: 'captured,needs_check,accepted,retake' },
  attention: { label: 'Needs a look', status: 'needs_check,retake' },
  pending: { label: 'Not taken yet', status: 'pending' },
  none: { label: 'No imagery, blocked or skipped', status: 'no_imagery,blocked,skipped' },
} as const;
type FilterKey = keyof typeof FILTERS;

function tone(s: Shot): Cone['tone'] {
  if (s.status === 'needs_check' || s.status === 'retake') return 'attention';
  if (s.status === 'no_imagery' || s.status === 'blocked' || s.status === 'skipped') return 'muted';
  if (s.status === 'accepted') return 'accepted';
  const side = s.side === 'left' ? 'left' : s.side === 'right' ? 'right' : 'none';
  return (s.status === 'captured' ? `done-${side}` : side) as Cone['tone'];
}

async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

export default function JobView({ id, setMap, setOverlay, onBack, onDeleted }: Props) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [mapShots, setMapShots] = useState<Shot[]>([]);
  const [tooFar, setTooFar] = useState(false);
  const [stage, setStage] = useState<'map' | 'photos' | 'activity'>('map');
  const [mapMode, setMapMode] = useState<'capture' | 'tagging'>('tagging');
  const activity = useTaggingActivity(id);
  const [tagging, setTagging] = useState<TaggingSummary | null>(null);
  const parallelSessions = useParallelSessions(activity.snapshot?.settings ?? tagging?.settings);
  const [tagSegments, setTagSegments] = useState<FC | null>(null);
  const rawTagSegments = useRef<FC | null>(null);
  const [mapFocus, setMapFocus] = useState<{ bounds: [number, number, number, number]; key: number } | null>(null);
  const [selectedSegment, setSelectedSegment] = useState<string | null>(null);
  const [tagError, setTagError] = useState('');
  const [tagLoading, setTagLoading] = useState(false);
  const tagFetching = useRef(false);
  const segmentRevision = useRef('');
  const [filter, setFilter] = useState<FilterKey>('saved');
  const [tagFilter, setTagFilter] = useState('all');
  const pageLimit = useRef(60);
  const [page, setPage] = useState<ShotPage>({ total: 0, items: [] });
  const [selected, setSelected] = useState<Shot | null>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [codexOpen, setCodexOpen] = useState(false);
  const [batch, setBatch] = useState(1);
  const [prompt, setPrompt] = useState<Prompt | null>(null);
  const [busy, setBusy] = useState('');
  const view = useRef<{ bbox: [number, number, number, number]; zoom: number } | null>(null);

  const load = useCallback(() => api.get<Job>(`/api/jobs/${id}`).then(setJob).catch((e) => setError(e.message)), [id]);
  useEffect(() => { load(); }, [load]);

  const loadTagging = useCallback(async () => {
    if (tagFetching.current) return;
    tagFetching.current = true;
    setTagLoading(true);
    try {
      const [summary, segments] = await Promise.all([
        api.get<TaggingSummary>(`/api/jobs/${id}/tagging`),
        api.get<FC>(`/api/jobs/${id}/tagging/segments`),
      ]);
      setTagging(summary);
      rawTagSegments.current = segments;
      const revision = segments.features.map((f) => {
        const p = f.properties;
        return `${p.segment_key}:${p.side}:${p.eligible}:${p.completed}:${p.running}:${p.queued}:${p.failed}:${p.needs_review}:${p.cancelled}`;
      }).join('|');
      if (revision !== segmentRevision.current) {
        segmentRevision.current = revision;
        setTagSegments(aggregateSegments(segments));
      }
      setTagError('');
      const current = selectedRef.current;
      if (current) {
        const fresh = await api.get<Shot>(`/api/shots/${current.id}`);
        setSelected((s) => s?.id === fresh.id ? fresh : s);
      }
    } catch (e: any) { setTagError(e.message); }
    finally { tagFetching.current = false; setTagLoading(false); }
  }, [id]);
  useEffect(() => { loadTagging(); const timer = setInterval(loadTagging, 5000); return () => clearInterval(timer); }, [loadTagging]);
  useEffect(() => {
    if (!selected?.id) return;
    let active = true;
    api.get<Shot>(`/api/shots/${selected.id}`).then((fresh) => { if (active) setSelected(fresh); }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [selected?.id]);

  // keep progress fresh while you capture in Chrome
  useEffect(() => {
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const small = (job?.counts.total ?? 0) <= SMALL_JOB;
  const done = job?.counts.done;

  const loadMapShots = useCallback(async () => {
    if (!job) return;
    if (small) {
      const p = await api.get<ShotPage>(`/api/jobs/${id}/shots?include_labels=false&limit=${SMALL_JOB}`);
      setMapShots(p.items);
      setTooFar(false);
      return;
    }
    const v = view.current;
    if (!v || v.zoom < MAP_MIN_ZOOM) {
      setMapShots([]);
      setTooFar(true);
      return;
    }
    const p = await api.get<ShotPage>(`/api/jobs/${id}/shots?include_labels=false&bbox=${v.bbox.map((n) => n.toFixed(6)).join(',')}&limit=4000`);
    setMapShots(p.items);
    setTooFar(false);
  }, [id, small, !!job, done]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { loadMapShots().catch((e) => setError(e.message)); }, [loadMapShots]);

  const loadPage = useCallback(async (append: boolean, offset = 0) => {
    const limit = append ? 60 : pageLimit.current;
    const p = await api.get<ShotPage>(`/api/jobs/${id}/shots?include_labels=false&status=${FILTERS[filter].status}${tagFilter === 'all' ? '' : `&tagging_status=${tagFilter}`}&order=recent&offset=${offset}&limit=${limit}`);
    if (append) pageLimit.current = offset + p.items.length;
    setPage((prev) => ({ total: p.total, items: append ? [...prev.items, ...p.items] : p.items }));
  }, [id, filter, tagFilter]);
  useEffect(() => { pageLimit.current = 60; }, [filter, tagFilter]);

  useEffect(() => { if (stage === 'photos') loadPage(false).catch((e) => setError(e.message)); }, [stage, loadPage, done, tagging?.counts.completed, tagging?.counts.queued, tagging?.counts.running, tagging?.counts.failed, tagging?.counts.cancelled]);

  const cones: Cone[] = useMemo(
    () => mapShots.map((s) => ({ id: s.id, lat: s.lat, lng: s.lng, heading: s.result?.camera_heading ?? s.heading, fov: s.fov, tone: tone(s), label: `${s.code}: ${STATUS_WORDS[s.status] ?? s.status}` })),
    [mapShots],
  );

  const onViewChange = useCallback((bbox: [number, number, number, number], zoom: number) => {
    view.current = { bbox, zoom };
    if (!small) loadMapShots().catch(() => undefined);
  }, [small, loadMapShots]);

  useEffect(() => {
    if (!job) return;
    setMap({
      area: job.area, streets: mapMode === 'tagging' ? null : job.streets, cones, drawMode: null, selectedId: selected?.id ?? null, fitKey: job.id, onViewChange,
      onConeClick: (sid) => setSelected(mapShots.find((s) => s.id === sid) ?? null),
      taggingSegments: mapMode === 'tagging' ? tagSegments : null, selectedSegment,
      focusBounds: mapFocus?.bounds, focusKey: mapFocus?.key,
      onSegmentClick: (key) => { setSelectedSegment(key); setSelected(null); },
    });
  }, [job, cones, selected, onViewChange, mapMode, tagSegments, selectedSegment, mapFocus]); // eslint-disable-line react-hooks/exhaustive-deps

  async function review(s: Shot, st: 'accepted' | 'retake' | 'pending') {
    const updated = await api.patch<Shot>(`/api/shots/${s.id}`, { status: st });
    setSelected(updated);
    setMapShots((xs) => xs.map((x) => (x.id === updated.id ? updated : x)));
    setPage((p) => ({ ...p, items: p.items.map((x) => (x.id === updated.id ? updated : x)) }));
    load();
  }

  // stage overlay: view switch, photo grid, shot detail
  useEffect(() => {
    if (!job) return;
    const c = job.counts;
    const n = (k: FilterKey) => FILTERS[k].status.split(',').reduce((t, st) => t + (c[st] ?? 0), 0);
    setOverlay(
      <>
        <div className="stage-switch seg" role="group" aria-label="Show">
          <button type="button" aria-pressed={stage === 'map'} onClick={() => setStage('map')}>Map</button>
          <button type="button" aria-pressed={stage === 'photos'} onClick={() => setStage('photos')} data-testid="show-photos">Photos ({n('saved').toLocaleString()})</button>
          <button type="button" aria-pressed={stage === 'activity'} onClick={() => setStage('activity')} data-testid="show-activity">Activity{activity.snapshot?.counts.running ? ` (${activity.snapshot.counts.running} running)` : ''}</button>
        </div>
        {stage === 'map' && <div className="map-mode seg" role="group" aria-label="Map layer"><button aria-pressed={mapMode === 'tagging'} onClick={() => setMapMode('tagging')}>Tagging progress</button><button aria-pressed={mapMode === 'capture'} onClick={() => setMapMode('capture')}>Capture</button></div>}
        {stage === 'map' && tooFar && <p className="stage-hint">{mapMode === 'tagging' ? 'Click a street section · zoom in for images' : 'Zoom in to see each photo'}</p>}
        {stage === 'map' && mapMode === 'tagging' && <TagLegend />}
        {stage === 'activity' && <TaggingActivity jobId={id} snapshot={activity.snapshot} summary={tagging} error={activity.error} connection={activity.connection} busy={!!busy} onOpen={openActivityPhoto} onCancel={cancelTagging} onRefresh={() => { activity.refresh(); loadTagging(); }} onChooseWork={() => document.querySelector('.tag-scope')?.scrollIntoView({ behavior: 'smooth', block: 'start' })} parallelSessions={parallelSessions} />}
        {stage === 'photos' && (
          <section className="photos" aria-label="Photos">
            <label className="photo-tag-filter field"><span>Tagging status</span><select aria-label="Filter photos by tagging status" value={tagFilter} onChange={(e) => setTagFilter(e.target.value)}><option value="all">All images</option><option value="unlabelled">Not tagged</option><option value="queued">Queued</option><option value="running">Running</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select></label>
            <div className="photo-filters seg" role="group" aria-label="Filter photos">
              {(Object.keys(FILTERS) as FilterKey[]).map((k) => (
                <button key={k} type="button" aria-pressed={filter === k} onClick={() => setFilter(k)}>{FILTERS[k].label} ({n(k).toLocaleString()})</button>
              ))}
            </div>
            {page.items.length === 0 ? (
              <p className="empty-stage">{tagFilter !== 'all' ? 'No images match these filters.' : filter === 'saved' ? 'No photos yet. Start the maply capture extension in Chrome and choose this job.' : 'Nothing here.'}</p>
            ) : (
              <>
                <ul className="grid">
                  {page.items.map((s) => (
                    <li key={s.id}>
                      <button type="button" className={`tile tile-${s.status}`} onClick={() => setSelected(s)} data-testid="photo-tile">
                        {s.thumb_url ? <img src={s.thumb_url} alt="" loading="lazy" /> : <span className="tile-empty">{STATUS_WORDS[s.status]}</span>}
                        <span className="tile-code">{s.code}</span>
                        <span className="tile-look">{s.look}</span><span className={`tile-tag tag-${tagStatus(s)}`}>{words(tagStatus(s))}{s.tagging?.needs_review ? " · needs review" : ""}</span>
                        {s.confidence && <span className="tile-confidence">{confidencePercent(s.confidence.mean)} mean confidence · {s.confidence.unknown} unknown</span>}
                        {s.flags.some((f) => ['far_from_plan', 'heading_off', 'wrong_side', 'file_missing', 'not_street_view'].includes(f)) && <span className="tile-flag">{flagWords(s)[0]}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
                {page.items.length < page.total && (
                  <button type="button" className="btn more-btn" onClick={() => loadPage(true, page.items.length)}>Show more ({(page.total - page.items.length).toLocaleString()} left)</button>
                )}
              </>
            )}
          </section>
        )}
        {selected && <ShotDetail key={selected.id} shot={selected} onClose={() => setSelected(null)} onReview={review} busy={!!busy} onTag={() => queueTagging({ shot_ids: [selected.id], retry: true })} />}
      </>,
    );
  }, [job, stage, filter, page, selected, tooFar, mapMode, busy, tagFilter, activity.snapshot, activity.error, activity.connection, tagging, parallelSessions.settings, parallelSessions.draft, parallelSessions.pending, parallelSessions.error]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => setOverlay(null), [setOverlay]);

  useEffect(() => {
    if (!codexOpen) return;
    api.get<Prompt>(`/api/jobs/${id}/prompt?batch=${batch}`).then(setPrompt).catch((e) => setError(e.message));
  }, [id, batch, codexOpen]);

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label);
    setError('');
    setNote('');
    try {
      await fn();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  }

  function openActivityPhoto(shotId: string) {
    api.get<Shot>(`/api/shots/${shotId}`).then(setSelected).catch((e) => setError(e.message));
  }

  function focusSelection(selector: TagSelector) {
    const features = rawTagSegments.current?.features.filter((f) => {
      const p = f.properties;
      return (!selector.side || p.side === selector.side) && (selector.segment_keys?.includes(p.segment_key) || selector.streets?.includes(p.street));
    }) ?? [];
    const points = features.flatMap((f) => f.geometry.type === 'MultiLineString' ? f.geometry.coordinates.flat() as number[][] : f.geometry.coordinates as number[][]);
    if (!points.length) { setError('No mapped section matches this selection and photographed side.'); return; }
    const bounds: [number, number, number, number] = [Infinity, Infinity, -Infinity, -Infinity];
    for (const [lng, lat] of points) {
      bounds[0] = Math.min(bounds[0], lng); bounds[1] = Math.min(bounds[1], lat);
      bounds[2] = Math.max(bounds[2], lng); bounds[3] = Math.max(bounds[3], lat);
    }
    setError('');
    setSelected(null);
    setStage('map');
    setMapMode('tagging');
    setMapFocus((previous) => ({ bounds, key: (previous?.key ?? 0) + 1 }));
  }

  function queueTagging(selector: TagSelector) {
    return run('tagging', async () => {
      const r = await api.post<{ queued: number; skipped: number }>(`/api/jobs/${id}/tagging`, selector);
      setNote(`${r.queued} image${r.queued === 1 ? '' : 's'} queued for tagging${r.skipped ? `; ${r.skipped} already processed, active or ineligible` : ''}.`);
      await loadTagging();
    });
  }
  function cancelTagging(selector: TagSelector) {
    return run('tagging', async () => {
      const r = await api.post<{ cancelled: number }>(`/api/jobs/${id}/tagging/cancel`, selector);
      setNote(`${r.cancelled} tagging task${r.cancelled === 1 ? '' : 's'} cancelled.`);
      await loadTagging();
    });
  }

  const check = () => run('check', async () => {
    const j = await api.post<Job>(`/api/jobs/${id}/check`);
    setJob(j);
    setNote(`${j.changed ? `${j.changed} photo${j.changed === 1 ? '' : 's'} updated.` : 'Nothing new.'}${j.unknown_log_entries?.length ? ` The log mentions ${j.unknown_log_entries.length} shots that aren’t in this job.` : ''}`);
  });

  const exportDataset = () => run('dataset', async () => {
    const r = await api.post<{ photos: number; manifest: string }>(`/api/jobs/${id}/dataset`);
    setNote(`Dataset written: ${r.photos.toLocaleString()} photos, listed in ${r.manifest}`);
  });

  const copyPrompt = (remaining: boolean) => run('copy', async () => {
    const p = remaining ? await api.get<Prompt>(`/api/jobs/${id}/prompt?remaining=true`) : prompt;
    if (p?.text && (await copy(p.text))) setNote(remaining ? `Prompt copied for ${p.shots.length} missing photos. Paste it into Codex.` : `Prompt ${batch} copied. Paste it into Codex.`);
  });

  async function remove() {
    if (!job) return;
    if (!window.confirm(`Delete “${job.name}” from maply? Next you can choose whether to keep its photos on disk.`)) return;
    const files = window.confirm('Also delete the job folder and its photos from disk? Choose Cancel to keep them.');
    await api.del(`/api/jobs/${id}?delete_files=${files}`);
    onDeleted();
  }

  if (!job) return <div className="panel-body">{error ? <p className="error">{error}</p> : <p className="hint">Loading…</p>}</div>;

  const c = job.counts;
  const left = c.total - c.done - (c.skipped ?? 0);

  return (
    <div className="panel-body">
      <button type="button" className="btn btn-quiet back" onClick={onBack}>All jobs</button>
      <h1 className="job-title">{job.name}</h1>
      <p className="muted">{c.total.toLocaleString()} photos{job.street_count ? ` along ${job.street_count.toLocaleString()} streets` : ''}</p>

      <div className="progress" data-testid="progress">
        <div className="progress-line"><strong>{c.done.toLocaleString()}</strong> of {c.total.toLocaleString()} done</div>
        <span className="meter" aria-hidden="true"><span style={{ width: `${c.total ? (100 * c.done) / c.total : 0}%` }} /></span>
        <p className="progress-detail">
          {[
            left > 0 && `${left.toLocaleString()} to go`,
            c.needs_check && `${c.needs_check.toLocaleString()} need a look`,
            c.no_imagery && `${c.no_imagery.toLocaleString()} no imagery`,
            c.blocked && `${c.blocked.toLocaleString()} blocked`,
            c.skipped && `${c.skipped.toLocaleString()} skipped`,
          ].filter(Boolean).join(', ')}
        </p>
        <p className="fineprint">
          {bytes(job.storage.saved_bytes)} of photos saved{job.storage.estimated_total_bytes ? `, about ${bytes(job.storage.estimated_total_bytes)} when finished` : ''}.
        </p>
      </div>

      <TaggingPanel jobId={id} summary={tagging} error={tagError} loading={tagLoading} busy={!!busy} selectedSegment={selectedSegment} segments={tagSegments} onSelectSegment={setSelectedSegment} onQueue={queueTagging} onCancel={cancelTagging} onPhoto={setSelected} onRefresh={loadTagging} onFocusMap={focusSelection} activity={<><ParallelSessions state={parallelSessions} /><ActivityPeek snapshot={activity.snapshot} onOpen={() => setStage('activity')} /></>} />
      {note && <p className="ok" role="status">{note}</p>}
      {error && <p className="error" role="alert">{error}</p>}

      <section className="step">
        <h2>Capture in Chrome</h2>
        <p className="hint">Open the maply capture extension, choose this job and press Start. Line up each view, then press Enter to save it and open the next. Stop whenever you like; it picks up where you left off.</p>
        <ul className="keys-inline">
          <li><kbd>Enter</kbd> save and next</li>
          <li><kbd>N</kbd> no imagery</li>
          <li><kbd>B</kbd> blocked</li>
          <li><kbd>X</kbd> skip</li>
          <li><kbd>[</kbd> back</li>
        </ul>
      </section>

      <section className="step">
        <h2>Dataset</h2>
        <p className="hint">Photos and their metadata are saved in this folder:</p>
        <div className="path">
          <code>{job.folder}</code>
          <button type="button" className="btn btn-quiet" onClick={() => copy(job.folder).then(() => setNote('Folder path copied.'))}>Copy</button>
        </div>
        <button type="button" className="btn btn-wide" onClick={exportDataset} disabled={!!busy} data-testid="export-dataset">{busy === 'dataset' ? 'Writing…' : 'Write dataset file'}</button>
        <p className="fineprint">Writes dataset/manifest.jsonl: one line per photo with its street, OSM tags, footpath side, planned and actual camera, imagery date, checks and current accessibility labels, including confidence and evidence.</p>
      </section>

      <section className="step">
        <button type="button" className="disclosure" aria-expanded={codexOpen} onClick={() => setCodexOpen(!codexOpen)}>Hand photos to Codex instead</button>
        {codexOpen && (
          <>
            {job.batches > 1 && (
              <div className="stepper" role="group" aria-label="Choose prompt">
                <button type="button" className="btn" disabled={batch <= 1} onClick={() => setBatch(batch - 1)} aria-label="Previous prompt">‹</button>
                <span>Prompt <strong>{batch.toLocaleString()}</strong> of {job.batches.toLocaleString()}</span>
                <button type="button" className="btn" disabled={batch >= job.batches} onClick={() => setBatch(batch + 1)} aria-label="Next prompt">›</button>
              </div>
            )}
            <pre className="prompt" tabIndex={0} aria-label={`Prompt ${batch}`}>{prompt?.text ?? ''}</pre>
            <button type="button" className="btn btn-wide" onClick={() => copyPrompt(false)} data-testid="copy-prompt">Copy prompt {job.batches > 1 ? batch : ''}</button>
            {c.done > 0 && left > 0 && <button type="button" className="btn btn-wide" onClick={() => copyPrompt(true)}>Copy prompt for missing photos</button>}
            <button type="button" className="btn btn-wide" onClick={check} disabled={!!busy} data-testid="check-photos">{busy === 'check' ? 'Checking…' : 'Check photos Codex saved'}</button>
            <p className="fineprint">Last checked {ago(job.checked_at)}.</p>
          </>
        )}
      </section>

      <button type="button" className="btn btn-quiet danger" onClick={remove}>Delete job</button>
    </div>
  );
}

function ShotDetail({ shot, onClose, onReview, busy, onTag }: { shot: Shot; onClose: () => void; onReview: (s: Shot, st: 'accepted' | 'retake' | 'pending') => void; busy: boolean; onTag: () => void }) {
  const [focused, setFocused] = useState<AccessibilityFeature | null>(null);
  const imageRef = useRef<HTMLDivElement>(null);
  const r = shot.result || {};
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  const flags = flagWords(shot);
  return (
    <aside className="detail" aria-label={`Photo ${shot.code}`} data-testid="shot-detail">
      <div className="detail-head">
        <h3>{shot.code}</h3>
        <span className={`pill pill-${shot.status}`}>{STATUS_WORDS[shot.status] ?? shot.status}</span>
        <button type="button" className="btn btn-quiet" onClick={onClose}>Close</button>
      </div>
      {shot.image_url ? (
        <ImageRegions imageUrl={shot.image_url} code={shot.code} features={shot.labels?.features ?? []} focused={focused?.id ?? null} onFocus={setFocused} imageRef={imageRef} />
      ) : (
        <div className="detail-img empty-img">No photo saved yet</div>
      )}
      <p className="detail-look">{shot.look}</p>
      <TagResults shot={shot} busy={busy} onQueue={onTag} focusedFeature={focused?.id ?? null} onFocus={(f) => { setFocused(f); imageRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }} />
      {flags.length > 0 && <ul className="flags">{flags.map((f) => <li key={f}>{f}</li>)}</ul>}
      <dl className="facts">
        <dt>Planned</dt>
        <dd>{shot.lat.toFixed(6)}, {shot.lng.toFixed(6)}, facing {shot.heading.toFixed(0)}°</dd>
        {r.camera_lat != null && (
          <>
            <dt>Camera</dt>
            <dd>{r.camera_offset_m} m from the planned spot, facing {r.camera_heading?.toFixed?.(0) ?? '?'}°{r.heading_delta != null ? ` (${r.heading_delta > 0 ? '+' : ''}${Math.round(r.heading_delta)}°)` : ''}</dd>
          </>
        )}
        {shot.near_node_m != null && (<><dt>Intersection</dt><dd>{Math.round(shot.near_node_m)} m away</dd></>)}
        {r.image_date && (<><dt>Imagery date</dt><dd>{r.image_date}</dd></>)}
        {shot.captured_at && (<><dt>Saved</dt><dd>{new Date(shot.captured_at).toLocaleString()}{shot.capture_source ? `, by ${shot.capture_source === 'extension' ? 'you in Chrome' : 'Codex'}` : ''}</dd></>)}
        {r.notes && (<><dt>Note</dt><dd>{r.notes}</dd></>)}
        {r.file && (<><dt>File</dt><dd className="break">{r.file}{r.width ? `, ${r.width}×${r.height}` : ''}</dd></>)}
      </dl>
      <p className="links">
        <a href={shot.url} target="_blank" rel="noreferrer">Open the planned view</a>
        {r.final_url && <a href={r.final_url} target="_blank" rel="noreferrer">Open the saved view</a>}
      </p>
      <p className="fineprint">The camera position is where Street View’s camera stood, not the location of anything in the photo.</p>
      <div className="detail-actions">
        {shot.status !== 'accepted' && shot.image_url && <button type="button" className="btn btn-primary" onClick={() => onReview(shot, 'accepted')}>Looks right</button>}
        {shot.status !== 'retake' && <button type="button" className="btn" onClick={() => onReview(shot, 'retake')}>Needs a retake</button>}
        {(shot.status === 'accepted' || shot.status === 'retake' || shot.status === 'skipped') && <button type="button" className="btn btn-quiet" onClick={() => onReview(shot, 'pending')}>Undo</button>}
      </div>
    </aside>
  );
}
