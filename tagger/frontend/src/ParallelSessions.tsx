import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type TaggingSettings } from './api';

export function useParallelSessions(live?: TaggingSettings) {
  const [settings, setSettings] = useState<TaggingSettings | null>(null);
  const [draft, setDraft] = useState(1);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const dirty = useRef(false);
  const saving = useRef(false);
  const loadFailed = useRef(false);
  const accept = useCallback((value: TaggingSettings) => {
    if (loadFailed.current) { loadFailed.current = false; setError(''); }
    if (saving.current) {
      setSettings((previous) => previous ? { ...previous, running: value.running } : value);
      return;
    }
    setSettings(value);
    if (!dirty.current) setDraft(value.parallel_sessions);
  }, []);
  const reload = useCallback(async () => {
    try { accept(await api.get<TaggingSettings>('/api/tagging/settings')); setError(''); }
    catch (e: any) { loadFailed.current = true; setError(e.message); }
  }, [accept]);
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => { if (live) accept(live); }, [live, accept]);
  const choose = (value: number) => {
    dirty.current = value !== settings?.parallel_sessions;
    setDraft(value);
    setError('');
  };
  const apply = async () => {
    if (!settings || saving.current || draft === settings.parallel_sessions) return;
    saving.current = true;
    setPending(true);
    setError('');
    try {
      const result = await api.patch<TaggingSettings>('/api/tagging/settings', { parallel_sessions: draft });
      setSettings(result);
      setDraft(result.parallel_sessions);
      dirty.current = false;
    } catch (e: any) { setError(e.message); }
    finally { saving.current = false; setPending(false); }
  };
  return { settings, draft, pending, error, choose, apply, reload };
}

export type ParallelSessionsState = ReturnType<typeof useParallelSessions>;

export default function ParallelSessions({ state }: { state: ParallelSessionsState }) {
  const { settings, draft, pending, error, choose, apply, reload } = state;
  const changed = !!settings && draft !== settings.parallel_sessions;
  return <section className="parallel-sessions" aria-label="Parallel session settings">
    <div className="tag-heading"><h3>Parallel sessions</h3><span className="fineprint">Across all jobs</span></div>
    <div className="parallel-controls"><select aria-label="Parallel sessions" value={draft} disabled={!settings || pending} onChange={(e) => choose(Number(e.target.value))}>{Array.from({ length: settings?.max_parallel_sessions ?? 20 }, (_, index) => index + 1).map((n) => <option key={n} value={n}>{n} session{n === 1 ? '' : 's'}</option>)}</select><button type="button" className="btn" disabled={!changed || pending} onClick={apply}>{pending ? 'Applying…' : 'Apply'}</button></div>
    <p className="parallel-effective" role="status">{settings ? `${settings.running} running · limit ${settings.parallel_sessions}${changed ? ` · ${draft} selected, not applied` : ''}` : error ? 'Session settings unavailable.' : 'Loading session settings…'}</p>
    {error && <div className="parallel-error"><p className="error" role="alert">{error}</p>{!settings && <button type="button" className="btn btn-quiet" onClick={reload}>Retry settings</button>}</div>}
    <p className="fineprint">More sessions start queued images together. Reducing the limit lets active work finish. Requests use your OpenRouter credits; more sessions may hit provider rate limits.</p>
  </section>;
}
