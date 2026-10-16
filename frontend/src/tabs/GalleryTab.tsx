import { useEffect, useMemo, useState, type ReactNode } from 'react';
import type { TabProps } from '../App';
import { api, qs } from '../lib/api';
import { useAction, useResource } from '../lib/hooks';
import type { Job, ObservationDetail, ObservationList, ObservationSummary, PlanDetail } from '../lib/types';
import { fmtBytes, fmtCoord, fmtDeg, fmtNum, fmtTime, humanize } from '../lib/format';
import { Chip, Copyable, Empty, ErrorList, Field, JobSelect, JsonBlock, KV, Loading, Mono, Notice } from '../components/common';
import { TERMINAL_JOB } from '../components/JobPanel';

const PAGE = 48;

export function GalleryTab({ project, active, jobId, setJobId }: TabProps) {
  const pid = project.id;
  const jobs = useResource(`jobs:${pid}`, () => api.get<Job[]>(`/api/projects/${pid}/jobs`), { interval: 15000, enabled: active });
  useEffect(() => {
    if (!jobId && jobs.data && jobs.data.length > 0) setJobId(jobs.data[0].id);
  }, [jobId, jobs.data, setJobId]);
  const job = jobs.data?.find((j) => j.id === jobId);
  const live = !!job && !TERMINAL_JOB.has(job.status);
  const plan = useResource(job ? `plan:${job.plan_id}` : null, () => api.get<PlanDetail>(`/api/plans/${job!.plan_id}`));

  const [status, setStatus] = useState('');
  const [flagDraft, setFlagDraft] = useState('');
  const [flag, setFlag] = useState('');
  const [side, setSide] = useState('');
  const [segment, setSegment] = useState('');
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [jobId, status, flag, side, segment]);

  const key = jobId ? `obs:${jobId}:${status}:${flag}:${side}:${segment}:${offset}` : null;
  const list = useResource(key, () => api.get<ObservationList>(`/api/jobs/${jobId}/observations${qs({ status, flag, side, segment_key: segment, limit: PAGE, offset })}`), {
    interval: live ? 8000 : null,
    enabled: active,
  });

  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => setSelected(null), [jobId]);
  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;

  const segments = useMemo(() => (plan.data?.geojson?.segments?.features ?? []).map((f) => f.properties), [plan.data]);
  const knownFlags = useMemo(() => [...new Set(items.flatMap((o) => o.quality_flags ?? []))].sort(), [items]);

  const idx = items.findIndex((o) => o.id === selected);
  const go = (d: number) => {
    const n = idx + d;
    if (n >= 0 && n < items.length) setSelected(items[n].id);
  };

  return (
    <div className="gallery-layout">
      <div className="toolbar toolbar-wrap" data-testid="gallery-filters">
        <label className="inline-label">
          Job
          <JobSelect jobs={jobs.data} value={jobId} onChange={setJobId} testid="gallery-job" />
        </label>
        <label className="inline-label">
          Verification
          <select value={status} onChange={(e) => setStatus(e.target.value)} data-testid="gallery-status">
            <option value="">all current</option>
            <option value="verified">verified</option>
            <option value="needs_review">needs review</option>
            <option value="rejected">rejected</option>
          </select>
        </label>
        <label className="inline-label">
          Flag
          <input
            list="gallery-flags"
            value={flagDraft}
            onChange={(e) => setFlagDraft(e.target.value)}
            onBlur={() => setFlag(flagDraft.trim())}
            onKeyDown={(e) => {
              if (e.key === 'Enter') setFlag(flagDraft.trim());
            }}
            placeholder="e.g. imagery_older_than_desired"
            data-testid="gallery-flag"
          />
          <datalist id="gallery-flags">
            {knownFlags.map((f) => (
              <option key={f} value={f} />
            ))}
          </datalist>
        </label>
        <label className="inline-label">
          Side
          <select value={side} onChange={(e) => setSide(e.target.value)} data-testid="gallery-side">
            <option value="">any</option>
            <option value="left">left</option>
            <option value="right">right</option>
            <option value="none">none</option>
          </select>
        </label>
        <label className="inline-label">
          Segment
          <select value={segment} onChange={(e) => setSegment(e.target.value)} data-testid="gallery-segment">
            <option value="">any</option>
            {segments.map((s) => (
              <option key={s.segment_key} value={s.segment_key}>
                {s.segment_key} {s.name ? `· ${s.name}` : ''}
              </option>
            ))}
          </select>
        </label>
        <span className="toolbar-spacer" />
        <span className="muted" data-testid="gallery-count">
          {list.data ? (total === 0 ? '0 observations' : `${offset + 1}–${Math.min(offset + PAGE, total)} of ${total}`) : ''}
        </span>
        <button type="button" className="btn btn-sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
          ‹ Prev
        </button>
        <button type="button" className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
          Next ›
        </button>
      </div>

      {jobs.data && jobs.data.length === 0 && <Empty>No jobs yet.</Empty>}
      {list.error && <ErrorList errors={list.error} />}
      {jobId && !list.data && !list.error && <Loading what="observations" />}
      {list.data && items.length === 0 && <Empty>No observations match these filters.</Empty>}
      <div className="gallery-grid" data-testid="gallery-grid">
        {items.map((o) => (
          <GalleryItem key={o.id} o={o} selected={o.id === selected} onClick={() => setSelected(o.id)} />
        ))}
      </div>

      {selected && (
        <ObservationDrawer
          id={selected}
          onClose={() => setSelected(null)}
          onPrev={idx > 0 ? () => go(-1) : undefined}
          onNext={idx >= 0 && idx < items.length - 1 ? () => go(1) : undefined}
          onChanged={() => void list.reload()}
        />
      )}
    </div>
  );
}

function GalleryItem({ o, selected, onClick }: { o: ObservationSummary; selected: boolean; onClick: () => void }) {
  const nflags = o.quality_flags?.length ?? 0;
  return (
    <button type="button" className={`gallery-item ${selected ? 'gallery-item-active' : ''} ${o.is_current ? '' : 'gallery-item-superseded'}`} onClick={onClick} data-testid="gallery-item" data-observation-id={o.id}>
      <div className="thumb">
        {o.thumb_url ? <img src={o.thumb_url} alt={`Observation ${o.id}`} loading="lazy" /> : <div className="thumb-missing">no thumbnail</div>}
        <span className="thumb-ordinal">#{o.sample_ordinal ?? '?'}</span>
        {nflags > 0 && (
          <span className="thumb-flags" title={o.quality_flags.join('\n')}>
            ⚑ {nflags}
          </span>
        )}
      </div>
      <div className="gallery-badges">
        <Chip value={o.verification_status} />
        <Chip value={o.association_status} />
      </div>
      <div className="gallery-meta">
        <span>
          {o.intended_side ?? '—'} / {humanize(o.view_kind)}
        </span>
        <span className="muted">{fmtDeg(o.heading_deg)}</span>
      </div>
    </button>
  );
}

function ObservationDrawer({ id, onClose, onPrev, onNext, onChanged }: { id: string; onClose: () => void; onPrev?: () => void; onNext?: () => void; onChanged: () => void }) {
  const d = useResource(`obsd:${id}`, () => api.get<ObservationDetail>(`/api/observations/${id}`));
  const act = useAction();
  const [note, setNote] = useState('');
  const [confirmAssoc, setConfirmAssoc] = useState(false);
  const [recapture, setRecapture] = useState(false);
  const [recapReason, setRecapReason] = useState('operator requested recapture');
  const [crop, setCrop] = useState({ x: '0', y: '0', width: '', height: '', purpose: 'analysis' });
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    setNote('');
    setConfirmAssoc(false);
    setRecapture(false);
    setMsg(null);
    act.setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return;
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft' && onPrev) onPrev();
      else if (e.key === 'ArrowRight' && onNext) onNext();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, onPrev, onNext]);

  const o = d.data;
  useEffect(() => {
    if (o?.image) setCrop((c) => (c.width === '' ? { ...c, width: String(o.image?.width ?? ''), height: String(o.image?.height ?? '') } : c));
  }, [o?.image]);

  const review = async (decision: 'accept' | 'reject') => {
    const body = decision === 'accept' ? { decision, note, confirm_association: confirmAssoc } : { decision, note, recapture };
    const r = await act.run(() => api.post<ObservationDetail>(`/api/observations/${id}/review`, body));
    if (r) {
      d.setData(r);
      setMsg(decision === 'accept' ? 'Accepted.' : recapture ? 'Rejected; recapture queued.' : 'Rejected.');
      onChanged();
    }
  };
  const requestRecapture = async () => {
    if (!o) return;
    const r = await act.run(() => api.post(`/api/tasks/${o.task_id}/recapture`, { view_ids: [o.planned_view_id], reason: recapReason || 'operator requested recapture' }));
    if (r) {
      setMsg('Recapture queued for this planned view (a completed job is reopened as paused; resume it in the Run tab).');
      void d.reload();
      onChanged();
    }
  };
  const makeCrop = async () => {
    const n = (s: string) => Number(s);
    const body = { x: n(crop.x), y: n(crop.y), width: n(crop.width), height: n(crop.height), purpose: crop.purpose || 'analysis' };
    if (![body.x, body.y, body.width, body.height].every((v) => Number.isInteger(v)) || body.x < 0 || body.y < 0 || body.width <= 0 || body.height <= 0) {
      act.setError(['Crop: x, y ≥ 0 and width, height > 0 (whole pixels)']);
      return;
    }
    const r = await act.run(() => api.post<{ id: string; url: string | null }>(`/api/observations/${id}/crops`, body));
    if (r) {
      setMsg('Analysis crop created (raw image unchanged).');
      void d.reload();
    }
  };

  const ori = o?.orientation;
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" data-testid="obs-detail" onClick={(e) => e.stopPropagation()} aria-label="Observation detail">
        <header className="drawer-head">
          <div>
            <strong>Observation</strong> {o && <Mono>{o.observation_id}</Mono>}
          </div>
          <div className="row gap">
            <button type="button" className="btn btn-sm" onClick={onPrev} disabled={!onPrev} aria-label="Previous observation">
              ‹
            </button>
            <button type="button" className="btn btn-sm" onClick={onNext} disabled={!onNext} aria-label="Next observation">
              ›
            </button>
            <button type="button" className="btn btn-sm" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </div>
        </header>
        <div className="drawer-body">
          {d.error && <ErrorList errors={d.error} />}
          {!o && !d.error && <Loading what="observation" />}
          {o && (
            <>
              <div className="obs-status">
                <Chip value={o.verification_status} testid="obs-verification" />
                <Chip value={o.association_status} />
                {!o.is_current && <Chip tone="muted">not current</Chip>}
                <span className="muted">
                  sample #{o.sample_ordinal ?? '?'} · {o.intended_side}/{humanize(o.view_kind)} · <Mono>{o.segment_key}</Mono>
                </span>
              </div>
              {o.image_url ? (
                <a href={o.image_url} target="_blank" rel="noopener noreferrer" className="obs-image" title="Open full-size image (local file) in a new tab">
                  <img src={o.image_url} alt="Captured view" />
                </a>
              ) : (
                <div className="shot-empty">Image file not available</div>
              )}
              {(o.quality_flags?.length ?? 0) > 0 && (
                <div className="chips">
                  {o.quality_flags!.map((f) => (
                    <Chip key={f} tone="warn">
                      ⚑ {humanize(f)}
                    </Chip>
                  ))}
                </div>
              )}

              {/* ---------------------------------------------- actions */}
              <div className="obs-actions">
                <Field label="Review note" htmlFor="obs-note">
                  <textarea id="obs-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} data-testid="review-note" />
                </Field>
                <div className="obs-action-row">
                  <label className="check">
                    <input type="checkbox" checked={confirmAssoc} onChange={(e) => setConfirmAssoc(e.target.checked)} data-testid="review-confirm-association" />I confirm the intended
                    footpath is visible
                  </label>
                  <button type="button" className="btn btn-ok" onClick={() => void review('accept')} disabled={act.busy || !o.is_current} data-testid="review-accept">
                    Accept
                  </button>
                </div>
                <div className="obs-action-row">
                  <label className="check">
                    <input type="checkbox" checked={recapture} onChange={(e) => setRecapture(e.target.checked)} data-testid="review-recapture" />
                    Request recapture
                  </label>
                  <button type="button" className="btn btn-danger" onClick={() => void review('reject')} disabled={act.busy || !o.is_current} data-testid="review-reject">
                    Reject
                  </button>
                </div>
                <div className="obs-action-row">
                  <input value={recapReason} onChange={(e) => setRecapReason(e.target.value)} aria-label="Recapture reason" placeholder="Recapture reason" />
                  <button type="button" className="btn" onClick={() => void requestRecapture()} disabled={act.busy} data-testid="obs-recapture">
                    Recapture this view
                  </button>
                </div>
                <details className="subsection">
                  <summary>Create analysis crop</summary>
                  <div className="crop-grid">
                    {(['x', 'y', 'width', 'height'] as const).map((k) => (
                      <label key={k} className="field">
                        <span>{k} (px)</span>
                        <input type="number" min={0} step={1} value={crop[k]} onChange={(e) => setCrop({ ...crop, [k]: e.target.value })} />
                      </label>
                    ))}
                    <label className="field">
                      <span>purpose</span>
                      <input value={crop.purpose} onChange={(e) => setCrop({ ...crop, purpose: e.target.value })} />
                    </label>
                  </div>
                  <button type="button" className="btn btn-sm" onClick={() => void makeCrop()} disabled={act.busy} data-testid="obs-crop">
                    Create crop
                  </button>
                  <p className="hint">Crops are derived files; the raw capture and its checksum are never modified.</p>
                </details>
                <ErrorList errors={act.error} title="Action refused" />
                {msg && <Notice tone="ok">{msg}</Notice>}
              </div>

              {/* ---------------------------------------------- metadata */}
              <MetaGroup title="Identifiers">
                <KV
                  rows={[
                    ['Observation', <Copyable key="o" text={o.observation_id} />],
                    ['Project', <Mono key="p">{o.project_id}</Mono>],
                    ['Job', <Mono key="j">{o.job_id}</Mono>],
                    ['Task', <Mono key="t">{o.task_id}</Mono>],
                    ['Attempt', <Mono key="a">{o.attempt_id ?? '—'}</Mono>],
                    ['Worker', <Mono key="w">{o.worker_id ?? '—'}</Mono>],
                    ['Plan', <Mono key="pl">{o.plan_id}</Mono>],
                    ['Segment', <Mono key="s">{`${o.segment_key ?? '—'} (${o.segment_id})`}</Mono>],
                    ['Sample', <Mono key="sm">{`#${o.sample_ordinal ?? '?'} (${o.sample_id})`}</Mono>],
                    ['Planned view', <Mono key="v">{o.planned_view_id}</Mono>],
                    ['Side convention', o.side_convention ?? null],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title="Positions">
                <Notice tone="warn">The camera position is where the source reports the panorama was taken. It is not the location of any kerb, ramp or obstruction visible in the image.</Notice>
                <KV
                  rows={[
                    ['Requested (sample)', o.requested_position ? `${fmtCoord(o.requested_position.lat)}, ${fmtCoord(o.requested_position.lng)}` : null],
                    ['', o.requested_position?.meaning ? <span className="muted small">{o.requested_position.meaning}</span> : null],
                    ['Camera (source-reported)', o.camera_position ? `${fmtCoord(o.camera_position.lat)}, ${fmtCoord(o.camera_position.lng)}` : 'not reported'],
                    ['Camera provenance', o.camera_position?.provenance ?? null],
                    ['Camera offset from sample', o.camera_offset_m !== null ? `${fmtNum(o.camera_offset_m, 2)} m` : null],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title="Panorama">
                <KV
                  rows={[
                    ['Source pano ID', o.panorama?.source_pano_id ? <Copyable key="p" text={o.panorama.source_pano_id} /> : null],
                    ['Provenance', o.panorama?.provenance ?? null],
                    ['Note', o.panorama?.note ?? null],
                    ['Source URL', o.source_url ? <Copyable key="u" text={o.source_url} /> : null],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title="Orientation">
                <table className="table table-compact">
                  <thead>
                    <tr>
                      <th />
                      <th className="right">Planned</th>
                      <th className="right">Observed</th>
                      <th>Observed provenance</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th>Heading</th>
                      <td className="right">{fmtDeg(ori?.planned?.heading_deg)}</td>
                      <td className="right">{fmtDeg(ori?.observed?.heading_deg)}</td>
                      <td>{ori?.observed?.heading_provenance ?? '—'}</td>
                    </tr>
                    <tr>
                      <th>Pitch</th>
                      <td className="right">{fmtDeg(ori?.planned?.pitch_deg)}</td>
                      <td className="right">{fmtDeg(ori?.observed?.pitch_deg)}</td>
                      <td>{ori?.observed?.pitch_provenance ?? '—'}</td>
                    </tr>
                    <tr>
                      <th>FOV</th>
                      <td className="right">{fmtDeg(ori?.planned?.fov_deg)}</td>
                      <td className="right">{fmtDeg(ori?.observed?.fov_deg)}</td>
                      <td>{ori?.observed?.fov_provenance ?? '—'}</td>
                    </tr>
                  </tbody>
                </table>
                <KV rows={[['Viewport', o.viewport ? `${o.viewport.width ?? '?'}×${o.viewport.height ?? '?'} @ ${o.viewport.device_scale_factor ?? 1}x` : null]]} />
              </MetaGroup>
              <MetaGroup title="Dates">
                <KV
                  rows={[
                    ['Imagery date (source-reported capture of the panorama)', o.imagery_date?.value ?? 'unknown'],
                    ['· precision', o.imagery_date?.precision ?? null],
                    ['· provenance', o.imagery_date?.provenance ?? null],
                    ['Collected at (when this tool took the screenshot)', fmtTime(o.collected_at)],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title="File">
                <KV
                  rows={[
                    ['Path (data dir relative)', o.image?.path ? <Copyable key="p" text={o.image.path} /> : null],
                    ['Format', o.image?.format ?? null],
                    ['Size', o.image ? `${o.image.width ?? '?'}×${o.image.height ?? '?'} px · ${fmtBytes(o.image.bytes)}` : null],
                    ['SHA-256', o.image?.sha256 ? <Copyable key="h" text={o.image.sha256} /> : null],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title="Method & provenance">
                <KV
                  rows={[
                    ['Capture method', o.capture_method],
                    ['Target', o.target],
                    ['Config version', <Mono key="c">{o.config_version_id ?? '—'}</Mono>],
                    ['Attribution', o.attribution],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title="Verification & review">
                <KV
                  rows={[
                    ['Verification status', <Chip key="v" value={o.verification_status} />],
                    ['Association status', <Chip key="a" value={o.association_status} />],
                    ['Quality flags', (o.quality_flags ?? []).length ? (o.quality_flags ?? []).join(', ') : 'none'],
                    ['Current', o.is_current ? 'yes' : 'no (superseded or rejected)'],
                    ['Review status', o.review?.status ?? null],
                    ['Review note', o.review?.note ?? null],
                    ['Reviewed at', o.review?.reviewed_at ? fmtTime(o.review.reviewed_at) : null],
                  ]}
                />
              </MetaGroup>
              <MetaGroup title={`Links (${o.links?.length ?? 0})`}>
                {(o.links ?? []).length === 0 ? (
                  <Empty>No links.</Empty>
                ) : (
                  <table className="table table-compact">
                    <thead>
                      <tr>
                        <th>Kind</th>
                        <th>Planned view</th>
                        <th>Association</th>
                        <th className="right">Camera→sample</th>
                        <th>Active</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(o.links ?? []).map((l, i) => (
                        <tr key={i}>
                          <td>{l.kind}</td>
                          <td>
                            <Mono>{l.planned_view_id.slice(0, 8)}</Mono>
                          </td>
                          <td>
                            <Chip value={l.association_status} />
                          </td>
                          <td className="right">{l.camera_to_sample_m !== null ? `${fmtNum(l.camera_to_sample_m, 2)} m` : '—'}</td>
                          <td>{l.active ? 'yes' : 'no'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </MetaGroup>
              <MetaGroup title={`Crops (${o.crops?.length ?? 0})`}>
                {(o.crops ?? []).length === 0 ? (
                  <Empty>No analysis crops.</Empty>
                ) : (
                  <ul className="crop-list">
                    {(o.crops ?? []).map((c) => (
                      <li key={c.id}>
                        {c.url ? (
                          <a href={c.url} target="_blank" rel="noopener noreferrer">
                            {c.purpose}
                          </a>
                        ) : (
                          c.purpose
                        )}{' '}
                        <span className="muted">
                          x {c.x}, y {c.y}, {c.width}×{c.height}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </MetaGroup>
              <JsonBlock value={o.verification_evidence} summary="Verification evidence (JSON)" />
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function MetaGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="meta-group">
      <h3>{title}</h3>
      {children}
    </section>
  );
}
