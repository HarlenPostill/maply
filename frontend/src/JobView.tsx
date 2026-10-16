import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, type Job, type Prompt, type Shot, type Status } from './api';
import type { Cone } from './MapView';
import type { MapState } from './App';
import { ago, flagWords, STATUS_WORDS } from './words';

type Props = {
  id: string;
  status: Status;
  setMap: (s: MapState) => void;
  setOverlay: (n: React.ReactNode) => void;
  onBack: () => void;
  onDeleted: () => void;
};

const DONE = new Set(['captured', 'accepted', 'no_imagery', 'blocked', 'needs_check']);

function tone(s: Shot): Cone['tone'] {
  if (s.status === 'needs_check' || s.status === 'retake') return 'attention';
  if (s.status === 'no_imagery' || s.status === 'blocked') return 'muted';
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
  const [batch, setBatch] = useState(1);
  const [prompt, setPrompt] = useState<Prompt | null>(null);
  const [copied, setCopied] = useState('');
  const [checking, setChecking] = useState(false);
  const [checkNote, setCheckNote] = useState('');
  const [stage, setStage] = useState<'map' | 'photos'>('map');
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState<'saved' | 'attention' | 'pending' | 'none'>('saved');

  const load = useCallback(() => api.get<Job>(`/api/jobs/${id}`).then(setJob).catch((e) => setError(e.message)), [id]);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    api.get<Prompt>(`/api/jobs/${id}/prompt?batch=${batch}`).then(setPrompt).catch((e) => setError(e.message));
    setCopied('');
  }, [id, batch]);

  const notDone = useMemo(() => (job?.shots ?? []).filter((s) => ['pending', 'retake', 'needs_check'].includes(s.status)).length, [job]);

  const cones: Cone[] = useMemo(
    () => (job?.shots ?? []).map((s) => ({ id: s.id, lat: s.lat, lng: s.lng, heading: s.result?.camera_heading ?? s.heading, fov: s.fov, tone: tone(s), label: `${s.code}: ${STATUS_WORDS[s.status] ?? s.status}` })),
    [job],
  );

  useEffect(() => {
    if (!job) return;
    setMap({ area: job.area, streets: job.streets, cones, drawMode: null, onConeClick: (sid) => setSelected(sid), selectedId: selected, fitKey: job.id });
  }, [job, cones, selected]); // eslint-disable-line react-hooks/exhaustive-deps

  const shotsById = useMemo(() => new Map((job?.shots ?? []).map((s) => [s.id, s])), [job]);

  async function review(s: Shot, status: 'accepted' | 'retake' | 'pending') {
    await api.patch(`/api/shots/${s.id}`, { status });
    await load();
  }

  // stage overlay: view switch, photo grid, shot detail
  useEffect(() => {
    if (!job) return;
    const counts = {
      saved: job.shots.filter((s) => s.result?.file).length,
      attention: job.shots.filter((s) => s.status === 'needs_check' || s.status === 'retake').length,
      pending: job.shots.filter((s) => s.status === 'pending').length,
      none: job.shots.filter((s) => s.status === 'no_imagery' || s.status === 'blocked').length,
    };
    const list = job.shots.filter((s) =>
      filter === 'saved' ? !!s.result?.file : filter === 'attention' ? s.status === 'needs_check' || s.status === 'retake' : filter === 'pending' ? s.status === 'pending' : s.status === 'no_imagery' || s.status === 'blocked',
    );
    const sel = selected ? shotsById.get(selected) : undefined;
    setOverlay(
      <>
        <div className="stage-switch seg" role="group" aria-label="Show">
          <button type="button" aria-pressed={stage === 'map'} onClick={() => setStage('map')}>Map</button>
          <button type="button" aria-pressed={stage === 'photos'} onClick={() => setStage('photos')} data-testid="show-photos">Photos ({counts.saved})</button>
        </div>
        {stage === 'photos' && (
          <section className="photos" aria-label="Photos">
            <div className="photo-filters seg" role="group" aria-label="Filter photos">
              <button type="button" aria-pressed={filter === 'saved'} onClick={() => setFilter('saved')}>Saved ({counts.saved})</button>
              <button type="button" aria-pressed={filter === 'attention'} onClick={() => setFilter('attention')}>Needs a look ({counts.attention})</button>
              <button type="button" aria-pressed={filter === 'pending'} onClick={() => setFilter('pending')}>Not taken yet ({counts.pending})</button>
              <button type="button" aria-pressed={filter === 'none'} onClick={() => setFilter('none')}>No imagery or blocked ({counts.none})</button>
            </div>
            {list.length === 0 ? (
              <p className="empty-stage">
                {filter === 'saved' ? 'No photos yet. Give Codex the first prompt, then press Check photos.' : 'Nothing here.'}
              </p>
            ) : (
              <ul className="grid">
                {list.map((s) => (
                  <li key={s.id}>
                    <button type="button" className={`tile tile-${s.status}`} onClick={() => setSelected(s.id)} data-testid="photo-tile">
                      {s.thumb_url ? <img src={s.thumb_url} alt="" loading="lazy" /> : <span className="tile-empty">{STATUS_WORDS[s.status]}</span>}
                      <span className="tile-code">{s.code}</span>
                      <span className="tile-look">{s.result?.look}</span>
                      {s.flags.some((f) => ['far_from_plan', 'heading_off', 'wrong_side', 'file_missing', 'not_street_view'].includes(f)) && <span className="tile-flag">{flagWords(s)[0]}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
        {sel && <ShotDetail shot={sel} onClose={() => setSelected(null)} onReview={review} />}
      </>,
    );
  }, [job, stage, filter, selected, shotsById]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => setOverlay(null), [setOverlay]);

  async function check() {
    setChecking(true);
    setCheckNote('');
    try {
      const before = job?.counts.done ?? 0;
      const j = await api.post<Job>(`/api/jobs/${id}/check`);
      setJob(j);
      const found = j.counts.done - before;
      const look = j.counts.needs_check ?? 0;
      setCheckNote(
        `${found > 0 ? `Found ${found} new photo${found === 1 ? '' : 's'}.` : 'No new photos.'}${look ? ` ${look} need${look === 1 ? 's' : ''} a look.` : ''}${j.unknown_log_entries?.length ? ` Codex logged ${j.unknown_log_entries.length} shot code${j.unknown_log_entries.length === 1 ? '' : 's'} that aren’t in this job.` : ''}`,
      );
    } catch (e: any) {
      setError(e.message);
    } finally {
      setChecking(false);
    }
  }

  async function copyPrompt(kind: 'batch' | 'retake') {
    try {
      const p = kind === 'batch' ? prompt : await api.get<Prompt>(`/api/jobs/${id}/prompt?remaining=true`);
      if (!p?.text) return;
      if (await copy(p.text)) setCopied(kind === 'batch' ? `Prompt ${batch} copied. Paste it into Codex.` : `Retake prompt copied (${p.shots.length} photos). Paste it into Codex.`);
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function remove() {
    if (!job) return;
    if (!window.confirm(`Delete “${job.name}” from maply? Next you can choose whether to keep its photos on disk.`)) return;
    const files = window.confirm('Also delete the job folder and its photos from disk? Choose Cancel to keep them.');
    await api.del(`/api/jobs/${id}?delete_files=${files}`);
    onDeleted();
  }

  if (!job) return <div className="panel-body">{error ? <p className="error">{error}</p> : <p className="hint">Loading…</p>}</div>;

  const c = job.counts;
  const batchDone = (b: number) => job.shots.filter((s) => s.batch === b).every((s) => DONE.has(s.status));

  return (
    <div className="panel-body">
      <button type="button" className="btn btn-quiet back" onClick={onBack}>All jobs</button>
      <h1 className="job-title">{job.name}</h1>
      <p className="muted">{c.total} photos in {job.batches} prompt{job.batches === 1 ? '' : 's'}</p>

      <div className="progress" data-testid="progress">
        <div className="progress-line"><strong>{c.done}</strong> of {c.total} photos taken</div>
        <span className="meter" aria-hidden="true"><span style={{ width: `${c.total ? (100 * c.done) / c.total : 0}%` }} /></span>
        {(c.needs_check || c.no_imagery || c.blocked) ? (
          <p className="progress-detail">
            {[c.needs_check && `${c.needs_check} need${c.needs_check === 1 ? 's' : ''} a look`, c.no_imagery && `${c.no_imagery} with no imagery`, c.blocked && `${c.blocked} blocked`].filter(Boolean).join(', ')}
          </p>
        ) : null}
      </div>

      <section className="step">
        <h2>Give Codex a prompt</h2>
        {job.batches > 1 && (
          <div className="stepper" role="group" aria-label="Choose prompt">
            <button type="button" className="btn" disabled={batch <= 1} onClick={() => setBatch(batch - 1)} aria-label="Previous prompt">‹</button>
            <span>
              Prompt <strong>{batch}</strong> of {job.batches}
              <span className={batchDone(batch) ? 'stepper-done' : 'muted'}>{batchDone(batch) ? ', all taken' : `, ${job.shots.filter((s) => s.batch === batch && DONE.has(s.status)).length} of ${job.shots.filter((s) => s.batch === batch).length} taken`}</span>
            </span>
            <button type="button" className="btn" disabled={batch >= job.batches} onClick={() => setBatch(batch + 1)} aria-label="Next prompt">›</button>
          </div>
        )}
        <pre className="prompt" tabIndex={0} aria-label={`Prompt ${batch}`}>{prompt?.text ?? ''}</pre>
        <button type="button" className="btn btn-primary btn-wide" onClick={() => copyPrompt('batch')} data-testid="copy-prompt">Copy prompt {job.batches > 1 ? batch : ''}</button>
        {c.done > 0 && notDone > 0 && (
          <>
            <button type="button" className="btn btn-wide" onClick={() => copyPrompt('retake')}>Copy prompt for missing photos</button>
            <p className="fineprint">Covers the next {Math.min(notDone, job.batch_size)} photos that haven’t been taken or need a retake.</p>
          </>
        )}
        {copied && <p className="ok" role="status">{copied}</p>}
      </section>

      <section className="step">
        <h2>Check saved photos</h2>
        <p className="hint">Codex saves photos and its log into this folder:</p>
        <div className="path">
          <code>{job.folder}</code>
          <button type="button" className="btn btn-quiet" onClick={() => copy(job.folder).then(() => setCopied('Folder path copied.'))}>Copy</button>
        </div>
        <button type="button" className="btn btn-wide" onClick={check} disabled={checking} data-testid="check-photos">{checking ? 'Checking…' : 'Check photos'}</button>
        <p className="fineprint">Last checked {ago(job.checked_at)}.</p>
        {checkNote && <p className="ok" role="status">{checkNote}</p>}
      </section>

      {error && <p className="error" role="alert">{error}</p>}
      <button type="button" className="btn btn-quiet danger" onClick={remove}>Delete job</button>
    </div>
  );
}

function ShotDetail({ shot, onClose, onReview }: { shot: Shot; onClose: () => void; onReview: (s: Shot, st: 'accepted' | 'retake' | 'pending') => void }) {
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
        <a href={shot.image_url} target="_blank" rel="noreferrer"><img className="detail-img" src={shot.image_url} alt={`Street View photo ${shot.code}`} /></a>
      ) : (
        <div className="detail-img empty-img">No photo saved yet</div>
      )}
      <p className="detail-look">{r.look}</p>
      {flags.length > 0 && (
        <ul className="flags">{flags.map((f) => <li key={f}>{f}</li>)}</ul>
      )}
      <dl className="facts">
        <dt>Planned</dt>
        <dd>{shot.lat.toFixed(6)}, {shot.lng.toFixed(6)}, facing {shot.heading.toFixed(0)}°</dd>
        {r.camera_lat != null && (
          <>
            <dt>Camera</dt>
            <dd>{r.camera_offset_m} m from the planned spot, facing {r.camera_heading?.toFixed?.(0) ?? '?'}°{r.heading_delta != null ? ` (${r.heading_delta > 0 ? '+' : ''}${Math.round(r.heading_delta)}°)` : ''}</dd>
          </>
        )}
        {r.image_date && (<><dt>Imagery date</dt><dd>{r.image_date}</dd></>)}
        {r.saved_at && (<><dt>Saved</dt><dd>{new Date(r.saved_at * 1000).toLocaleString()}</dd></>)}
        {r.notes && (<><dt>Codex’s note</dt><dd>{r.notes}</dd></>)}
        {r.file && (<><dt>File</dt><dd className="break">{r.file}{r.width ? `, ${r.width}×${r.height}` : ''}</dd></>)}
      </dl>
      <p className="links">
        <a href={shot.url} target="_blank" rel="noreferrer">Open the planned view</a>
        {r.final_url && <a href={r.final_url} target="_blank" rel="noreferrer">Open Codex’s lined-up view</a>}
      </p>
      <p className="fineprint">The camera position is where Street View’s camera stood, not the location of anything in the photo.</p>
      <div className="detail-actions">
        {shot.status !== 'accepted' && shot.image_url && <button type="button" className="btn btn-primary" onClick={() => onReview(shot, 'accepted')}>Looks right</button>}
        {shot.status !== 'retake' && <button type="button" className="btn" onClick={() => onReview(shot, 'retake')}>Needs a retake</button>}
        {(shot.status === 'accepted' || shot.status === 'retake') && <button type="button" className="btn btn-quiet" onClick={() => onReview(shot, 'pending')}>Undo</button>}
      </div>
    </aside>
  );
}
