import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type ActivityItem, type ActivitySnapshot, type TagCounts, type TaggingSummary, type TagSelector, type UsageSnapshot } from './api';
import { confidencePercent, ConfidenceCard } from './Confidence';
import { words } from './Tagging';
import ParallelSessions, { type ParallelSessionsState } from './ParallelSessions';

export function useTaggingActivity(jobId: string) {
  const [snapshot, setSnapshot] = useState<ActivitySnapshot | null>(null);
  const [error, setError] = useState('');
  const [connection, setConnection] = useState<'connecting' | 'live' | 'polling'>('connecting');
  const latest = useRef<ActivitySnapshot | null>(null);
  const streamHealthy = useRef(false);
  const accept = useCallback((value: ActivitySnapshot) => {
    latest.current = value;
    setSnapshot(value);
    setError('');
  }, []);
  const refresh = useCallback(async () => {
    try { accept(await api.get<ActivitySnapshot>(`/api/jobs/${jobId}/tagging/activity?limit=50`)); }
    catch (e: any) { setError(e.message); }
  }, [jobId, accept]);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    streamHealthy.current = false;
    setConnection('connecting');
    const source = new EventSource(`/api/jobs/${jobId}/tagging/activity/stream?limit=50`);
    source.onopen = () => { if (!disposed) { streamHealthy.current = true; setConnection('live'); } };
    source.addEventListener('activity', (event) => {
      if (disposed) return;
      try {
        accept(JSON.parse((event as MessageEvent).data));
        streamHealthy.current = true;
        setConnection('live');
      } catch { setError('The activity stream returned an unreadable update.'); }
    });
    source.onerror = () => { if (!disposed) { streamHealthy.current = false; setConnection('polling'); } };
    const poll = async () => {
      if (disposed) return;
      if (!streamHealthy.current) await refresh();
      if (!disposed) timer = setTimeout(poll, (latest.current?.counts.running || latest.current?.counts.queued) ? 2000 : 5000);
    };
    poll();
    return () => { disposed = true; clearTimeout(timer); source.close(); };
  }, [jobId, refresh, accept]);
  return { snapshot, error, connection, refresh };
}

function duration(start: string | null, now: number) {
  if (!start) return 'Starting';
  const seconds = Math.max(0, Math.floor((now - Date.parse(start)) / 1000));
  if (!Number.isFinite(seconds)) return 'Running';
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
}
function dateTime(value: string | null | undefined) {
  return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : 'Not reported';
}
function stageOf(item: ActivityItem) {
  const events = item.events.filter((e) => e.kind === 'stage');
  return events.at(-1)?.message || (item.status === 'running' ? 'Waiting for provider update' : words(item.status));
}

export function UsageCard({ usage }: { usage: UsageSnapshot | null | undefined }) {
  return <section className="usage-card" aria-label="Antigravity usage">
    <div className="tag-heading"><h3>Antigravity allowance</h3><span className="fineprint">Provider reported</span></div>
    {!usage || usage.status !== 'available' ? <><p className="muted">Allowance unavailable</p><p className="fineprint">{usage?.error || 'The worker has not reported account allowance yet.'}</p></> : usage.groups.length ? usage.groups.map((group, index) => <div className="quota-group" key={`${group.name}-${index}`}><strong>{group.name}</strong>{group.buckets.map((bucket) => <div className="quota-bucket" key={bucket.id}><div><span>{bucket.name}</span><strong>{bucket.remaining_fraction == null ? 'Unavailable' : `${Math.round(bucket.remaining_fraction * 100)}% remaining`}</strong></div>{bucket.remaining_fraction != null && <meter min="0" max="1" value={bucket.remaining_fraction} aria-label={`${bucket.name} remaining allowance`} />}<small>{bucket.window || 'Window not reported'} · resets {dateTime(bucket.reset_time)}</small></div>)}</div>) : <p className="muted">No allowance buckets reported.</p>}
    <p className="fineprint">{usage ? `Checked ${dateTime(usage.fetched_at)} · ${usage.source}` : 'Waiting for the vision worker'}</p>
    <p className="fineprint">Allowance is shared with other account usage. Image capacity is not estimated.</p>
  </section>;
}

function TaskActions({ item, busy, onOpen, onCancel }: { item: ActivityItem; busy: boolean; onOpen: (id: string) => void; onCancel: (selector: TagSelector) => void }) {
  return <div className="task-actions"><button type="button" className="btn" onClick={() => onOpen(item.shot_id)}>Open image</button>{['queued', 'running'].includes(item.status) && <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => onCancel({ shot_ids: [item.shot_id] })}>Cancel</button>}</div>;
}

function EventList({ item }: { item: ActivityItem }) {
  return <ol className="activity-events" aria-label={`Activity for ${item.code}`}>{item.events.map((event) => <li key={event.id} className={`event-${event.kind}`}><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time><div><strong>{event.message || words(event.kind)}</strong>{event.kind === 'usage' && event.data.usage && typeof event.data.usage === 'object' ? <p>{Object.entries(event.data.usage as Record<string, unknown>).filter(([, n]) => typeof n === 'number').map(([k, n]) => `${words(k)}: ${Number(n).toLocaleString()}`).join(' · ')}</p> : null}{typeof event.data.output_text === 'string' && <pre>{event.data.output_text}</pre>}</div></li>)}</ol>;
}

function RunningTask({ item, now, busy, onOpen, onCancel }: { item: ActivityItem; now: number; busy: boolean; onOpen: (id: string) => void; onCancel: (selector: TagSelector) => void }) {
  return <article className="running-task">
    <div className="tag-heading"><strong>{item.code}</strong><span className="tag-badge tag-running">Running · {duration(item.started_at, now)}</span></div>
    <p>{item.street || 'Unnamed street'} · {item.side}</p>
    <div className="running-stage"><i /><strong>{stageOf(item)}</strong></div>
    <p className="fineprint">{item.model || 'Model not reported'} · attempt {item.attempts}</p>
    {item.events.length > 0 && <EventList item={item} />}
    <TaskActions item={item} busy={busy} onOpen={onOpen} onCancel={onCancel} />
  </article>;
}

export function ActivityPeek({ snapshot, onOpen }: { snapshot: ActivitySnapshot | null; onOpen: () => void }) {
  const running = snapshot?.items.find((item) => item.status === 'running');
  return <div className="activity-peek"><div><strong>{running ? `Analysing ${running.code}` : snapshot?.counts.queued ? `${snapshot.counts.queued} images queued` : 'Live tagging activity'}</strong><span>{running ? stageOf(running) : 'Inspect the queue, results and account allowance.'}</span></div><button type="button" className="btn" onClick={onOpen}>Open live queue</button></div>;
}

export function TaggingActivity({ jobId, snapshot, summary, error, connection, busy, onOpen, onCancel, onRefresh, onChooseWork, parallelSessions }: {
  parallelSessions: ParallelSessionsState;
  jobId: string; snapshot: ActivitySnapshot | null; summary: TaggingSummary | null; error: string;
  connection: 'connecting' | 'live' | 'polling'; busy: boolean;
  onOpen: (id: string) => void; onCancel: (selector: TagSelector) => void; onRefresh: () => void; onChooseWork: () => void;
}) {
  const [now, setNow] = useState(Date.now());
  const [offset, setOffset] = useState(0);
  const [queue, setQueue] = useState<ActivitySnapshot | null>(null);
  const [recent, setRecent] = useState<ActivityItem[]>([]);
  const [listError, setListError] = useState('');
  const [listLoading, setListLoading] = useState(false);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const counts = snapshot?.counts ?? summary?.counts;
  const countKey = JSON.stringify(counts);
  const queueKey = snapshot?.items.filter((item) => item.status === 'queued').map((item) => item.task_id).join(',');
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  useEffect(() => {
    let active = true;
    setListLoading(true);
    Promise.all([
      api.get<ActivitySnapshot>(`/api/jobs/${jobId}/tagging/activity?status=queued&limit=20&offset=${offset}`),
      api.get<ActivitySnapshot>(`/api/jobs/${jobId}/tagging/activity?status=completed,failed,cancelled&limit=10&offset=${historyOffset}`),
    ]).then(([q, h]) => { if (active) { setQueue(q); setRecent(h.items); setHistoryTotal(h.total); setListError(''); if (offset > 0 && q.total <= offset) setOffset(Math.max(0, Math.floor((q.total - 1) / 20) * 20)); } }).catch((e) => { if (active) setListError(e.message); }).finally(() => { if (active) setListLoading(false); });
    return () => { active = false; };
  }, [jobId, offset, historyOffset, countKey, queueKey, refreshVersion]);
  const running = snapshot?.items.filter((item) => item.status === 'running') ?? [];
  return <section className="activity-stage" aria-label="Live tagging activity">
    <header className="activity-header"><div><p className="eyebrow">VISION WORKSPACE</p><h2>Live tagging activity</h2><p className="muted">Follow each image from the queue to its observations.</p></div><div className="activity-connection"><span className={`connection-dot connection-${connection}`} />{connection === 'live' ? 'Live updates' : connection === 'polling' ? 'Polling updates' : 'Connecting…'}<button type="button" className="btn" onClick={() => { setRefreshVersion((n) => n + 1); onRefresh(); }}>Refresh</button><button type="button" className="btn btn-primary" onClick={onChooseWork}>Choose streets to tag</button></div></header>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="activity-metrics">{([['running', 'Running'], ['queued', 'Queued'], ['completed', 'Completed'], ['failed', 'Failed']] as [keyof TagCounts, string][]).map(([key, label]) => <div key={key}><strong>{counts?.[key]?.toLocaleString() ?? '—'}</strong><span>{label}</span></div>)}</div>
    <div className="activity-layout"><div className="activity-main">
      <section className="activity-section"><div className="tag-heading"><h3>Running now</h3><span className="fineprint">Elapsed time is not a completion estimate</span></div>{running.length ? running.map((item) => <RunningTask key={item.task_id} item={item} now={now} busy={busy} onOpen={onOpen} onCancel={onCancel} />) : <p className="activity-empty">{counts?.queued ? summary?.worker.online ? 'The worker is preparing the next image.' : 'The queue will resume when the vision worker connects.' : 'No images are being analysed. Select streets or a section to queue work.'}</p>}</section>
      <section className="activity-section"><div className="tag-heading"><h3>Up next</h3><span className="fineprint">{queue?.total ?? counts?.queued ?? 0} queued</span></div>{listError && <p className="error">{listError}</p>}{queue?.items.length ? <ol className="queue-list">{queue.items.map((item) => <li key={item.task_id}><span className="queue-position" title="Position in this job's queue">{item.queue_position ?? '·'}</span><div><strong>{item.code}</strong><p>{item.street || 'Unnamed street'} · {item.side}</p></div><TaskActions item={item} busy={busy} onOpen={onOpen} onCancel={onCancel} /></li>)}</ol> : <p className="activity-empty">{listLoading ? 'Loading queue…' : 'The queue is empty.'}</p>}{(queue?.total ?? 0) > 20 && <div className="activity-pagination"><button className="btn" disabled={offset === 0 || listLoading} onClick={() => setOffset((n) => Math.max(0, n - 20))}>Previous</button><span>{offset + 1}–{Math.min(offset + 20, queue!.total)} of {queue!.total}</span><button className="btn" disabled={offset + 20 >= queue!.total || listLoading} onClick={() => setOffset((n) => n + 20)}>Next</button></div>}</section>
      <section className="activity-section"><div className="tag-heading"><h3>Recent outcomes</h3><span className="fineprint">Newest first</span></div>{recent.length ? <div className="recent-results">{recent.map((item) => <article key={item.task_id}><div className="tag-heading"><strong>{item.code}</strong><span className={`tag-badge tag-${item.status}`}>{words(item.status)}</span></div><p>{item.street} · {item.side}</p>{item.confidence && <p className="result-confidence">{confidencePercent(item.confidence.mean)} mean model confidence · {item.confidence.unknown} unknown</p>}{item.error && <p className="error">{item.error}</p>}<p className="fineprint">{dateTime(item.updated_at)}</p>{item.events.length > 0 && <details><summary>View activity</summary><EventList item={item} /></details>}<TaskActions item={item} busy={busy} onOpen={onOpen} onCancel={onCancel} /></article>)}</div> : <p className="activity-empty">Completed, failed and cancelled images will appear here.</p>}{historyTotal > 10 && <div className="activity-pagination"><button className="btn" disabled={!historyOffset || listLoading} onClick={() => setHistoryOffset((n) => Math.max(0, n - 10))}>Newer</button><span>{historyOffset + 1}–{Math.min(historyOffset + 10, historyTotal)} of {historyTotal}</span><button className="btn" disabled={historyOffset + 10 >= historyTotal || listLoading} onClick={() => setHistoryOffset((n) => n + 10)}>Older</button></div>}</section>
    </div><aside className="activity-aside"><ParallelSessions state={parallelSessions} /><UsageCard usage={snapshot?.usage} /><ConfidenceCard confidence={summary?.confidence} images={summary?.counts.completed} eligible={summary?.counts.eligible} aggregate /></aside></div>
  </section>;
}
