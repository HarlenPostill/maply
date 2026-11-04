import { useCallback, useEffect, useState } from 'react';
import { api } from './api';

type Config = {
  model: string; fallback_models: string[]; max_photos: number; max_saved_photos: number; max_requests: number;
  max_cost_usd: number; hours: number; max_steps: number; min_confidence: number; load_seconds: number; yolo?: boolean;
};
type Run = { id: string; status: string; config: Config; requests: number; cost_usd: number;
  finished: number; saved: number; review: number; marked: number; error: string | null; expires_at: string };
type Task = { shot_id: string; code: string; status: string; worker_id: string; lease_until: string; error: string | null; steps: number; has_evidence: boolean };
type Snapshot = { runs: Run[]; tasks: Task[]; managed?: boolean };
type NumericConfigKey = { [K in keyof Config]-?: Config[K] extends number ? K : never }[keyof Config];
const defaults: Config = { model: 'google/gemini-3.1-flash-lite', fallback_models: [], max_photos: 200, max_saved_photos: 0,
  max_requests: 1000, max_cost_usd: 5, hours: 8, max_steps: 6, min_confidence: .9, load_seconds: 4, yolo: false };

export default function CapturePanel({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot>({ runs: [], tasks: [] });
  const [config, setConfig] = useState<Config>(defaults);
  const [fallbacks, setFallbacks] = useState('');
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [error, setError] = useState('');
  const [catalogError, setCatalogError] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const load = useCallback(async () => {
    const state = await api.get<Snapshot>(`/api/capture/jobs/${jobId}`);
    setSnapshot(state);
  }, [jobId]);
  useEffect(() => {
    let active = true;
    const refresh = () => api.get<Snapshot>(`/api/capture/jobs/${jobId}`).then((state) => { if (active) setSnapshot(state); })
      .catch((e) => { if (active) setError(e.message); });
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [jobId]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    api.get<{ models: { id: string; name: string }[] }>('/api/tagging/provider/models')
      .then((value) => { if (active) setModels(value.models); })
      .catch(() => { if (active) setCatalogError('Model list unavailable. Enter a full OpenRouter model ID.'); });
    return () => { active = false; };
  }, [open]);
  async function perform(fn: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await fn(); await load(); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    await perform(async () => {
      const value = { ...config, model: config.model.trim(), fallback_models: fallbacks.split(',').map((s) => s.trim()).filter(Boolean) };
      if (editing) await api.put(`/api/capture/runs/${editing}`, value);
      else await api.post('/api/capture/runs', { ...value, job_id: jobId });
      setEditing(null);
    });
  }
  const number = (key: NumericConfigKey, label: string, min: number, max: number, step = 1) => <label>{label}
    <input type="number" required min={min} max={max} step={step} value={config[key]}
      onChange={(e) => setConfig({ ...config, [key]: Number(e.target.value) })} /></label>;
  return <section className="step capture-panel">
    <button type="button" className="disclosure" aria-expanded={open} onClick={() => setOpen(!open)}>Automatic capture in Chrome</button>
    <p className="hint">{snapshot.managed ? 'The cloud browser' : 'A desktop worker'} inspects each view, adjusts it with the mouse, and saves it. Choose any OpenRouter image model with structured output. Ready views are captured and tagged in one request. Uncertain views go to your manual backlog.</p>
    {open && <>
      <form onSubmit={submit}>
        <fieldset disabled={busy}>
          <legend>{editing ? 'Edit paused run' : 'New capture run'}</legend>
          <label>Capture and tagging model<input required list="capture-models" value={config.model} onChange={(e) => setConfig({ ...config, model: e.target.value })} /></label>
          <datalist id="capture-models">{models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</datalist>
          {catalogError && <p className="fineprint">{catalogError}</p>}
          <label>Fallback models, separated by commas<input value={fallbacks} onChange={(e) => setFallbacks(e.target.value)} placeholder="Optional OpenRouter model IDs" /></label>
          <p className="fineprint">Fallbacks handle provider failures. To use a discounted model, pause a run, edit its model, then resume.</p>
          {number('max_photos', 'Photos to handle (including review)', 1, 100000)}
          {number('max_saved_photos', 'Saved photo target (0 = no separate target)', 0, 100000)}
          <p className="fineprint">Only saved photos outside the review queue count toward this target. Reviews and no-imagery outcomes do not. The other limits can still stop a run earlier.</p>
          {number('max_requests', 'Maximum model requests', 1, 1000000)}
          {number('max_cost_usd', 'Stop after reported cost (USD)', .01, 10000, .01)}
          {number('hours', 'Run duration (hours)', .1, 72, .1)}
          {number('max_steps', 'Maximum assessments per photo', 1, 20)}
          {number('min_confidence', 'Minimum confidence to save or mark', .5, 1, .01)}
          {number('load_seconds', 'Initial loading wait (seconds)', 2, 30, 1)}
          <label className="check"><input type="checkbox" checked={!!config.yolo} onChange={(e) => setConfig({ ...config, yolo: e.target.checked })} /> YOLO mode</label>
          <p className="fineprint">YOLO runs never pause themselves: errors, worker crashes, service restarts and model stop requests are logged and the run continues. Only the photo, request, cost and duration goals, or your own Pause/Stop, end it.</p>
          <p className="fineprint">Uses the OpenRouter key saved in Model settings. Cost limits use provider-reported USD charges; a final request or requests already in flight may exceed the limit. Missing cost reporting pauses the run. Request limits also apply. Separate tagging retries are billed outside the capture run limit.</p>
          <button className="btn btn-primary" type="submit">{busy ? 'Saving…' : editing ? 'Save changes' : 'Create capture run'}</button>
          {editing && <button className="btn btn-quiet" type="button" onClick={() => setEditing(null)}>Cancel edit</button>}
        </fieldset>
      </form>
      {snapshot.managed ? <p className="hint">Capture runs on the cloud desktops, even when your laptop is off. All connected workers share one run and its limits. Create or resume a run here to start it; pause or stop it to let another run start.</p> : <>
        <p className="hint">On the computer doing capture, keep Chrome open on its dedicated desktop and run the command below. On macOS, give the terminal Accessibility, Automation and Screen Recording permissions. Keep the desktop awake and unlocked.</p>
        <p className="fineprint">Supports macOS and Linux X11. Each desktop runs one worker. Additional machines need their own graphical session and an SSH tunnel to this Maply server.</p>
      </>}
      {snapshot.runs.map((run) => <article className="capture-run" key={run.id}>
        <p><strong>{run.status}</strong>{run.config.yolo && ' · YOLO'} · {run.saved.toLocaleString()} saved · {run.review.toLocaleString()} for review · {run.marked.toLocaleString()} marked · {run.finished.toLocaleString()}/{run.config.max_photos.toLocaleString()} handled · {run.requests.toLocaleString()} requests · US${run.cost_usd.toFixed(4)}</p>
        <p className="fineprint">{run.config.model} · Ends {new Date(run.expires_at).toLocaleString()}</p>
        {!!run.config.max_saved_photos && <p className="fineprint">Saved photo target: {run.saved.toLocaleString()} / {run.config.max_saved_photos.toLocaleString()}</p>}
        {run.error && <p role="status">{run.error}</p>}
        {!snapshot.managed && <div className="path"><code>cd backend &amp;&amp; uv run --extra capture python ../scripts/capture_worker.py --run {run.id}</code></div>}
        {['running', 'paused'].includes(run.status) && <div className="actions">
          <button type="button" className="btn" disabled={busy} onClick={() => perform(() => api.post(`/api/capture/runs/${run.id}/${run.status === 'running' ? 'pause' : 'resume'}`, {}))}>{run.status === 'running' ? 'Pause' : 'Resume'}</button>
          {run.status === 'paused' && <button type="button" className="btn" disabled={busy} onClick={() => { setEditing(run.id); setConfig({ ...defaults, ...run.config }); setFallbacks(run.config.fallback_models.join(', ')); }}>Edit model and limits</button>}
          <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => perform(() => api.post(`/api/capture/runs/${run.id}/stop`, {}))}>Stop run</button>
        </div>}
      </article>)}
      {snapshot.tasks.some((t) => t.status === 'leased') && <details><summary>Active desktop workers</summary>
        {snapshot.tasks.filter((t) => t.status === 'leased').map((task) => <div className="capture-task" key={task.shot_id}>
          <strong>{task.code}</strong><p className="fineprint">{task.worker_id} · {task.steps} assessments · reserved until {new Date(task.lease_until).toLocaleTimeString()}</p>
        </div>)}
      </details>}
    </>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
