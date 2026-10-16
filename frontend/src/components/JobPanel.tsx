import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { api, qs } from '../lib/api';
import { useAction, useNow, useResource } from '../lib/hooks';
import type { Coverage, Job, JobEvent, TaskDetail, TaskRow, WorkerCommandRow, WorkerInfo } from '../lib/types';
import { fmtClock, fmtDeg, fmtDuration, fmtNum, fmtTime, humanize, shortId } from '../lib/format';
import { Chip, CountChips, Empty, ErrorList, JsonBlock, KV, Loading, Mono, Notice, Progress, Section, confirmAction } from './common';

export const TERMINAL_JOB = new Set(['completed', 'cancelled']);
export const TASK_STATES = ['planned', 'queued', 'running', 'captured', 'verified', 'needs_review', 'no_imagery', 'manual_required', 'failed', 'cancelled'];

export function JobPanel({ jobId, active, onChanged }: { jobId: string; active: boolean; onChanged: () => void }) {
  const [status, setStatus] = useState<string | undefined>(undefined);
  const live = !TERMINAL_JOB.has(status ?? '');
  const job = useResource(`job:${jobId}`, () => api.get<Job>(`/api/jobs/${jobId}`), { interval: live ? 2000 : null, enabled: active });
  const coverage = useResource(`cov:${jobId}`, () => api.get<Coverage>(`/api/jobs/${jobId}/coverage`), { interval: live ? 4000 : null, enabled: active });
  const [refreshKey, setRefreshKey] = useState(0);
  const act = useAction();

  // Track status; when it changes (e.g. job completes) do one final refresh of everything.
  const prevStatus = useRef<string | undefined>(undefined);
  const jd = job.data;
  useEffect(() => {
    const s = jd?.status;
    setStatus(s);
    if (prevStatus.current !== undefined && s !== prevStatus.current) {
      void coverage.reload();
      setRefreshKey((k) => k + 1);
      onChanged();
    }
    prevStatus.current = s;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jd?.status]);

  // Local runtime tick while running.
  const fetchedAt = useRef(Date.now());
  useEffect(() => {
    fetchedAt.current = Date.now();
  }, [jd]);
  const running = jd?.status === 'running';
  const now = useNow(1000, running && active);
  const runtime = jd ? jd.runtime_s + (running ? Math.max(0, (now - fetchedAt.current) / 1000) : 0) : null;

  const doAction = async (action: 'start' | 'pause' | 'resume' | 'cancel') => {
    if (action === 'cancel' && !confirmAction('Cancel this job? Queued tasks will be cancelled and the job cannot be restarted. Captured observations are kept.')) return;
    const res = await act.run(() => api.post<Job>(`/api/jobs/${jobId}/${action}`));
    if (res) {
      job.setData(res);
      void coverage.reload();
      setRefreshKey((k) => k + 1);
      onChanged();
    }
  };

  const refreshAll = () => {
    void job.reload();
    void coverage.reload();
    setRefreshKey((k) => k + 1);
  };

  if (job.error && !jd) return <ErrorList errors={job.error} title="Could not load job" />;
  if (!jd) return <Loading what="job" />;

  const cov = coverage.data;
  const mu = jd.model_usage;
  const vp = jd.settings?.viewport;
  const canStart = jd.status === 'draft';
  const canPause = jd.status === 'running';
  const canResume = jd.status === 'paused';
  const canCancel = ['running', 'paused', 'draft'].includes(jd.status);
  const maxDur = jd.max_duration_s || 0;
  const pauseIsManual = jd.pause_kind === 'manual_intervention';

  return (
    <div className="job-panel" data-testid="job-panel">
      <Section
        title={
          <>
            {jd.name || shortId(jd.id)} <Chip value={jd.status} testid="job-status" />
          </>
        }
        actions={
          <>
            <button type="button" className="btn btn-primary" data-testid="job-start" disabled={!canStart || act.busy} onClick={() => void doAction('start')}>
              Start
            </button>
            <button type="button" className="btn" data-testid="job-pause" disabled={!canPause || act.busy} onClick={() => void doAction('pause')}>
              Pause
            </button>
            <button type="button" className="btn" data-testid="job-resume" disabled={!canResume || act.busy} onClick={() => void doAction('resume')}>
              Resume job
            </button>
            <button type="button" className="btn btn-danger" data-testid="job-cancel" disabled={!canCancel || act.busy} onClick={() => void doAction('cancel')}>
              Cancel
            </button>
            <button type="button" className="btn btn-ghost" onClick={refreshAll} title="Refresh now">
              ↻
            </button>
          </>
        }
      >
        <ErrorList errors={act.error} title="Action refused" />
        {jd.pause_reason && (
          <div className={`pause-banner ${pauseIsManual ? 'pause-manual' : 'pause-other'}`} data-testid="pause-banner" role="status">
            <div className="pause-kind">{pauseIsManual ? 'MANUAL INTERVENTION REQUIRED' : `PAUSED — ${humanize(jd.pause_kind ?? 'paused').toUpperCase()}`}</div>
            <div className="pause-reason">{jd.pause_reason}</div>
            {pauseIsManual && <div className="pause-hint">Resolve in the worker panel, then Resume.</div>}
          </div>
        )}
        <div className="meta-line">
          <span>
            <span className="muted">target</span> <strong>{jd.target}</strong>
          </span>
          <span>
            <span className="muted">method</span> <strong>{jd.method}</strong>
          </span>
          <span>
            <span className="muted">workers</span> <strong>{jd.worker_count}</strong>
          </span>
          <span>
            <span className="muted">rate</span> <strong>{fmtNum(jd.requests_per_minute, 1)}/min</strong>
          </span>
          <span>
            <span className="muted">viewport</span> <strong>{vp ? `${vp.width}×${vp.height}` : '—'}</strong>
          </span>
          <span>
            <span className="muted">plan</span> <Mono>{shortId(jd.plan_id)}</Mono>
          </span>
          <span>
            <span className="muted">config</span> <Mono>{shortId(jd.config_version_id)}</Mono>
          </span>
          <span>
            <span className="muted">created</span> {fmtTime(jd.created_at)}
          </span>
          {jd.started_at && (
            <span>
              <span className="muted">started</span> {fmtTime(jd.started_at)}
            </span>
          )}
          {jd.finished_at && (
            <span>
              <span className="muted">finished</span> {fmtTime(jd.finished_at)}
            </span>
          )}
        </div>

        <div className="progress-pair">
          <Progress
            testid="progress-completeness"
            label="Collection completeness — tasks finished (any outcome)"
            value={cov?.collection?.completeness}
            sub={cov ? `${cov.collection.tasks_terminal} of ${cov.collection.tasks_total} tasks in a terminal state. This is not coverage.` : coverage.error ? coverage.error.join('; ') : 'loading…'}
          />
          <Progress
            testid="progress-verified"
            tone="ok"
            label="Verified coverage — planned views with a verified capture"
            value={cov?.verified_coverage}
            sub={cov ? `${cov.views?.verified ?? 0} of ${cov.views?.planned_views ?? 0} planned views verified` : undefined}
          />
          <Progress
            tone="teal"
            label="Covered incl. reuse and pending review"
            value={cov?.covered_including_reuse_and_review}
            sub="verified + reused panorama + needs review (not all confirmed)"
          />
        </div>

        <div className="job-grid">
          <div>
            <h3>Task states</h3>
            <CountChips counts={jd.task_counts} order={TASK_STATES} testid="task-counts" />
            <KV
              rows={[
                ['Attempts (total)', fmtNum(jd.attempts_total)],
                ['Elapsed runtime', <span key="rt" data-testid="job-runtime">{fmtDuration(runtime)}</span>],
              ]}
            />
          </div>
          <div>
            <h3>Limits</h3>
            <LimitRow label="Captures" cur={jd.captures_current} max={jd.max_captures} fmt={(x) => fmtNum(x)} />
            <LimitRow label="Runtime" cur={runtime ?? 0} max={maxDur} fmt={(x) => fmtDuration(x)} />
            <LimitRow label="Est. model spend" cur={mu?.estimated_cost_usd ?? 0} max={jd.model_budget_usd} fmt={(x) => `$${x.toFixed(4)}`} />
          </div>
          <div>
            <h3>
              Model usage <Chip tone="warn">Estimate</Chip>
            </h3>
            <KV
              rows={[
                ['Calls', fmtNum(mu?.calls)],
                ['Input tokens', fmtNum(mu?.input_tokens)],
                ['Cached input tokens', fmtNum(mu?.cached_input_tokens)],
                ['Output tokens', fmtNum(mu?.output_tokens)],
                ['Images sent', fmtNum(mu?.images_sent)],
                ['Estimated cost', mu ? `$${mu.estimated_cost_usd.toFixed(4)}` : null],
              ]}
            />
            {mu?.label && <p className="hint">{mu.label}</p>}
          </div>
          <div>
            <h3>Egress (this job)</h3>
            <KV
              rows={[
                ['Allowed', fmtNum(jd.egress?.allowed)],
                ['Blocked', <span key="b" className={jd.egress?.blocked ? 'bad-text' : undefined}>{fmtNum(jd.egress?.blocked)}</span>],
              ]}
            />
            <JsonBlock value={jd.settings} summary="Job settings (policy, viewport, mock)" />
          </div>
        </div>
      </Section>

      <Section title={`Workers (${jd.workers?.length ?? 0})`}>
        {(!jd.workers || jd.workers.length === 0) && <Empty>No workers registered for this job yet. Workers appear after the job starts.</Empty>}
        <div className="workers">
          {(jd.workers ?? []).map((w) => (
            <WorkerCard key={w.id} w={w} job={jd} active={active} />
          ))}
        </div>
      </Section>

      <TasksTable jobId={jobId} jobStatus={jd.status} active={active} live={live} refreshKey={refreshKey} onChanged={refreshAll} />
      <EventsLog jobId={jobId} active={active} live={live} refreshKey={refreshKey} />
    </div>
  );
}

function LimitRow({ label, cur, max, fmt }: { label: string; cur: number; max: number; fmt: (x: number) => string }) {
  const frac = max > 0 ? Math.min(1, cur / max) : 0;
  const tone = frac >= 1 ? 'bad' : frac >= 0.8 ? 'warn' : 'ok';
  return (
    <div className="limit-row">
      <div className="limit-label">
        <span>{label}</span>
        <span>
          <strong>{fmt(cur)}</strong> / {max > 0 ? fmt(max) : 'no limit'}
        </span>
      </div>
      <div className="mini-track">
        <div className={`mini-fill mini-${tone}`} style={{ width: `${frac * 100}%` }} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- workers

function WorkerCard({ w, job, active }: { w: WorkerInfo; job: Job; active: boolean }) {
  const awaiting = w.status === 'awaiting_operator';
  const hbStale = w.status !== 'stopped' && w.heartbeat_age_s !== null && w.heartbeat_age_s > 30;
  const ct = w.current_task;
  return (
    <div className={`worker-card worker-${w.status}`} data-testid="worker-card" data-worker-id={w.id} data-status={w.status}>
      <div className="worker-head">
        <Mono>{w.id}</Mono>
        <Chip value={w.status} />
        {w.method && <span className="muted">{w.method}</span>}
      </div>
      <div className="worker-meta">
        <span className={hbStale ? 'bad-text' : undefined} title={w.heartbeat_at ?? undefined}>
          heartbeat {w.heartbeat_age_s === null ? '—' : `${w.heartbeat_age_s.toFixed(0)} s ago`}
          {hbStale && ' ⚠ stale'}
        </span>
        <span className="muted">
          {w.host}
          {w.pid ? ` · pid ${w.pid}` : ''}
          {w.browser_profile ? ` · ${w.browser_profile}` : ''}
        </span>
      </div>
      {w.message && <div className="worker-msg">{w.message}</div>}
      {ct ? (
        <div className="worker-task">
          <div>
            Task <strong>#{ct.ordinal}</strong> · <Mono>{ct.segment_key}</Mono> · attempt {ct.attempt} · <Chip value={ct.status} />
          </div>
          <ul className="view-list">
            {ct.views.map((v) => (
              <li key={v.id}>
                {v.side}/{humanize(v.view_kind)} @ {fmtDeg(v.heading_deg)} — {v.captured ? <Chip value={v.captured} /> : <span className="muted">not captured</span>}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="muted small">No current task</div>
      )}
      {awaiting ? (
        <ManualControl w={w} job={job} active={active} />
      ) : (
        <Screenshot w={w} />
      )}
    </div>
  );
}

function Screenshot({ w, onClick, marker }: { w: WorkerInfo; onClick?: (e: MouseEvent<HTMLImageElement>) => void; marker?: { x: number; y: number } | null }) {
  if (!w.latest_screenshot_url) return <div className="shot-empty">No screenshot yet</div>;
  return (
    <figure className="shot">
      <div className={`shot-wrap ${onClick ? 'shot-clickable' : ''}`}>
        <img src={w.latest_screenshot_url} alt={`Latest screenshot from ${w.id}`} data-testid="worker-screenshot" onClick={onClick} draggable={false} />
        {marker && <span className="shot-marker" style={{ left: `${marker.x * 100}%`, top: `${marker.y * 100}%` }} />}
      </div>
      <figcaption>
        Latest screenshot · {fmtTime(w.latest_screenshot_at)} · local file
      </figcaption>
    </figure>
  );
}

function ManualControl({ w, job, active }: { w: WorkerInfo; job: Job; active: boolean }) {
  const cmds = useResource(`cmds:${w.id}`, () => api.get<WorkerCommandRow[]>(`/api/workers/${encodeURIComponent(w.id)}/commands?limit=12`), { interval: 2000, enabled: active });
  const act = useAction();
  const [marker, setMarker] = useState<{ x: number; y: number } | null>(null);
  const [reason, setReason] = useState('');
  const vpW = job.settings?.viewport?.width;
  const vpH = job.settings?.viewport?.height;
  const isManual = job.method === 'manual';

  const send = async (command: string, payload: Record<string, unknown> = {}) => {
    const r = await act.run(() => api.post<{ id: number; status: string }>(`/api/workers/${encodeURIComponent(w.id)}/commands`, { command, payload }));
    if (r) void cmds.reload();
  };

  const onShotClick = (e: MouseEvent<HTMLImageElement>) => {
    const img = e.currentTarget;
    // Scale against the content box (exclude the CSS border) so clicks map exactly.
    const rect = img.getBoundingClientRect();
    const cw = img.clientWidth;
    const ch = img.clientHeight;
    if (cw <= 0 || ch <= 0) return;
    const fx = Math.max(0, Math.min(1, (e.clientX - rect.left - img.clientLeft) / cw));
    const fy = Math.max(0, Math.min(1, (e.clientY - rect.top - img.clientTop) / ch));
    const W = vpW || img.naturalWidth;
    const H = vpH || img.naturalHeight;
    const x = Math.max(0, Math.min(W - 1, Math.round(fx * W)));
    const y = Math.max(0, Math.min(H - 1, Math.round(fy * H)));
    setMarker({ x: fx, y: fy });
    window.setTimeout(() => setMarker(null), 1500);
    void send('click', { x, y });
  };

  return (
    <div className="manual" data-testid="manual-control">
      <Notice tone="warn">
        <strong>Manual control.</strong> Workers never bypass CAPTCHAs or sign-ins; you resolve them here, then press <strong>Resume job</strong>. Click the screenshot to
        click in the worker&apos;s browser (scaled to the {vpW && vpH ? `${vpW}×${vpH}` : 'browser'} viewport).
      </Notice>
      <Screenshot w={w} onClick={onShotClick} marker={marker} />
      <div className="row gap wrap manual-keys">
        {['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Escape'].map((k) => (
          <button key={k} type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void send('key', { key: k })} data-testid={`key-${k}`}>
            {k === 'ArrowLeft' ? '← Left' : k === 'ArrowRight' ? '→ Right' : k === 'ArrowUp' ? '↑ Up' : k === 'ArrowDown' ? '↓ Down' : 'Esc'}
          </button>
        ))}
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void send('refresh')} data-testid="cmd-refresh">
          Refresh
        </button>
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void send('skip', reason ? { reason } : {})} data-testid="cmd-skip">
          Skip task
        </button>
      </div>
      {isManual && w.current_task && (
        <div className="manual-views">
          <table className="table table-compact">
            <thead>
              <tr>
                <th>Planned view</th>
                <th>Heading</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {w.current_task.views.map((v) => (
                <tr key={v.id}>
                  <td>
                    {v.side}/{humanize(v.view_kind)}
                  </td>
                  <td>{fmtDeg(v.heading_deg)}</td>
                  <td>{v.captured ? <Chip value={v.captured} /> : <span className="muted">—</span>}</td>
                  <td className="right nowrap">
                    <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void send('goto_view', { view_id: v.id })}>
                      Go to view
                    </button>{' '}
                    <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void send('capture', { view_id: v.id })}>
                      Capture
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="row gap wrap">
            <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void send('no_imagery', { reason: reason || 'operator: no usable imagery' })}>
              No imagery
            </button>
            <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void send('done')}>
              Done
            </button>
          </div>
        </div>
      )}
      <label className="field">
        <span>Reason (for skip / no imagery)</span>
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="optional" />
      </label>
      <ErrorList errors={act.error} title="Command refused" />
      <div className="cmd-log">
        <h4>Recent commands</h4>
        {cmds.error && <ErrorList errors={cmds.error} />}
        {cmds.data && cmds.data.length === 0 && <div className="muted small">No commands sent yet</div>}
        {cmds.data && cmds.data.length > 0 && (
          <table className="table table-compact">
            <thead>
              <tr>
                <th>#</th>
                <th>Command</th>
                <th>Payload</th>
                <th>Status</th>
                <th>Result</th>
                <th>Sent</th>
              </tr>
            </thead>
            <tbody>
              {cmds.data.map((c) => (
                <tr key={c.id}>
                  <td>{c.id}</td>
                  <td>{c.command}</td>
                  <td className="mono small">{JSON.stringify(c.payload)}</td>
                  <td>
                    <Chip value={c.status} />
                  </td>
                  <td className="mono small">{c.result === null || c.result === undefined ? '—' : JSON.stringify(c.result)}</td>
                  <td className="nowrap">{fmtClock(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- tasks

function TasksTable({ jobId, jobStatus, active, live, refreshKey, onChanged }: { jobId: string; jobStatus: string; active: boolean; live: boolean; refreshKey: number; onChanged: () => void }) {
  const [filter, setFilter] = useState('');
  const tasks = useResource(`tasks:${jobId}:${filter}`, () => api.get<TaskRow[]>(`/api/jobs/${jobId}/tasks${qs({ status: filter })}`), {
    interval: live ? 4000 : null,
    enabled: active,
  });
  const [expanded, setExpanded] = useState<string | null>(null);
  const act = useAction();
  const reloadTasks = tasks.reload;
  useEffect(() => {
    if (refreshKey > 0) void reloadTasks();
  }, [refreshKey, reloadTasks]);

  const retry = async (t: TaskRow, kind: 'retry' | 'recapture') => {
    const r = await act.run(() => (kind === 'retry' ? api.post<TaskRow>(`/api/tasks/${t.id}/retry`) : api.post<TaskRow>(`/api/tasks/${t.id}/recapture`, {})));
    if (r) {
      void tasks.reload();
      onChanged();
    }
  };

  const list = tasks.data ?? [];
  const jobCancelled = jobStatus === 'cancelled';
  return (
    <Section
      title={`Tasks${tasks.data ? ` (${list.length})` : ''}`}
      actions={
        <label className="inline-label">
          Status
          <select value={filter} onChange={(e) => setFilter(e.target.value)} data-testid="task-filter">
            <option value="">all</option>
            {TASK_STATES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </label>
      }
    >
      <ErrorList errors={act.error} title="Task action refused" />
      {tasks.error && <ErrorList errors={tasks.error} />}
      {!tasks.data && !tasks.error && <Loading what="tasks" />}
      {tasks.data && list.length === 0 && <Empty>No tasks{filter ? ` with status ${humanize(filter)}` : ''}.</Empty>}
      {list.length > 0 && (
        <div className="table-scroll table-scroll-tall">
          <table className="table table-compact" data-testid="tasks-table">
            <thead>
              <tr>
                <th className="right">#</th>
                <th>Segment</th>
                <th>Status</th>
                <th className="right">Attempts</th>
                <th className="right">Failures</th>
                <th>Reason</th>
                <th>Lease</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.map((t) => {
                const busyStatus = t.status === 'running' || t.status === 'manual_required';
                return (
                  <TaskRowView
                    key={t.id}
                    t={t}
                    expanded={expanded === t.id}
                    onToggle={() => setExpanded(expanded === t.id ? null : t.id)}
                    actions={
                      <>
                        {(t.status === 'failed' || t.status === 'no_imagery') && (
                          <button type="button" className="btn btn-sm" disabled={act.busy || jobCancelled} onClick={() => void retry(t, 'retry')}>
                            Retry
                          </button>
                        )}{' '}
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost"
                          disabled={act.busy || busyStatus || jobCancelled}
                          title={busyStatus ? 'Task is leased by a worker' : 'Queue all planned views of this sample again'}
                          onClick={() => void retry(t, 'recapture')}
                        >
                          Recapture
                        </button>
                      </>
                    }
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function TaskRowView({ t, expanded, onToggle, actions }: { t: TaskRow; expanded: boolean; onToggle: () => void; actions: ReactNode }) {
  const detail = useResource(expanded ? `task:${t.id}:${t.attempt_count}:${t.status}` : null, () => api.get<TaskDetail>(`/api/tasks/${t.id}`));
  return (
    <>
      <tr className={expanded ? 'row-expanded' : undefined}>
        <td className="right">
          <button type="button" className="btn-link" onClick={onToggle} aria-expanded={expanded} title="Show attempts">
            {expanded ? '▾' : '▸'} {t.ordinal}
          </button>
        </td>
        <td>
          <Mono>{t.segment_key}</Mono>
        </td>
        <td>
          <Chip value={t.status} />
          {t.view_filter && t.view_filter.length > 0 && (
            <span className="muted small" title={t.view_filter.join(', ')}>
              {' '}
              ({t.view_filter.length} view{t.view_filter.length === 1 ? '' : 's'})
            </span>
          )}
        </td>
        <td className="right">
          {t.attempt_count}/{t.max_attempts}
        </td>
        <td className="right">{t.failure_count}</td>
        <td className="reason-cell" title={t.outcome_reason ?? t.last_error ?? undefined}>
          {t.outcome_reason ?? t.last_error ?? <span className="muted">—</span>}
        </td>
        <td>{t.lease_owner ? <Mono>{t.lease_owner}</Mono> : <span className="muted">—</span>}</td>
        <td className="right nowrap">{actions}</td>
      </tr>
      {expanded && (
        <tr className="row-detail">
          <td colSpan={8}>
            {detail.error && <ErrorList errors={detail.error} />}
            {!detail.data && !detail.error && <Loading what="attempts" />}
            {detail.data && (
              <div className="task-detail">
                <table className="table table-compact">
                  <thead>
                    <tr>
                      <th>Attempt</th>
                      <th>Worker</th>
                      <th>Outcome</th>
                      <th>Error</th>
                      <th>Started</th>
                      <th>Ended</th>
                      <th className="right">Model calls</th>
                      <th className="right">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.attempts.map((a) => (
                      <tr key={a.attempt_no}>
                        <td>{a.attempt_no}</td>
                        <td>
                          <Mono>{a.worker_id}</Mono>
                        </td>
                        <td>
                          <Chip value={a.outcome} />
                        </td>
                        <td>{a.error ?? <span className="muted">—</span>}</td>
                        <td className="nowrap">{fmtTime(a.started_at)}</td>
                        <td className="nowrap">{fmtTime(a.ended_at)}</td>
                        <td className="right">{a.model_calls}</td>
                        <td className="right">{a.actions}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <JsonBlock value={detail.data.result} summary="Task result" />
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------- events

function EventsLog({ jobId, active, live, refreshKey }: { jobId: string; active: boolean; live: boolean; refreshKey: number }) {
  const events = useResource(`events:${jobId}`, () => api.get<JobEvent[]>(`/api/jobs/${jobId}/events?limit=200`), { interval: live ? 3000 : null, enabled: active });
  const [level, setLevel] = useState('');
  const reloadEvents = events.reload;
  useEffect(() => {
    if (refreshKey > 0) void reloadEvents();
  }, [refreshKey, reloadEvents]);
  const list = (events.data ?? []).filter((e) => !level || e.level === level);
  return (
    <Section
      title="Events"
      actions={
        <label className="inline-label">
          Level
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">all</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="error">error</option>
          </select>
        </label>
      }
    >
      {events.error && <ErrorList errors={events.error} />}
      {!events.data && !events.error && <Loading what="events" />}
      {events.data && list.length === 0 && <Empty>No events.</Empty>}
      {list.length > 0 && (
        <div className="table-scroll table-scroll-tall">
          <table className="table table-compact events" data-testid="events-log">
            <tbody>
              {list.map((e) => (
                <tr key={e.id} className={`ev ev-${e.level}`}>
                  <td className="nowrap mono small">{fmtTime(e.created_at)}</td>
                  <td>
                    <span className={`lvl lvl-${e.level}`}>{e.level}</span>
                  </td>
                  <td className="mono small">{e.kind}</td>
                  <td>{e.message}</td>
                  <td className="mono small muted">{e.worker_id ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
