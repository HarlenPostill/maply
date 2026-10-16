import { useEffect, useMemo, useState } from 'react';
import type { TabProps } from '../App';
import { api, qs } from '../lib/api';
import { useAction, useResource } from '../lib/hooks';
import type { Job, JobCreate, Plan } from '../lib/types';
import { fmtNum, fmtTime, shortId } from '../lib/format';
import { Chip, Empty, ErrorList, Field, Loading, Notice, Section } from '../components/common';
import { JobPanel } from '../components/JobPanel';

const METHODS: JobCreate['method'][] = ['deterministic_browser', 'openai_agent', 'manual', 'streetview_static_api'];
const TARGETS: JobCreate['target'][] = ['synthetic', 'google_maps'];

interface JobForm {
  name: string;
  target: JobCreate['target'];
  method: JobCreate['method'];
  worker_count: string;
  requests_per_minute: string;
  max_captures: string;
  max_duration_minutes: string;
  model_budget_usd: string;
  max_attempts: string;
  mock: string;
}

const DEFAULT_FORM: JobForm = {
  name: '',
  target: 'synthetic',
  method: 'deterministic_browser',
  worker_count: '1',
  requests_per_minute: '6',
  max_captures: '200',
  max_duration_minutes: '60',
  model_budget_usd: '0',
  max_attempts: '3',
  mock: '',
};

function buildJob(f: JobForm): { body: JobCreate | null; errors: string[] } {
  const errors: string[] = [];
  const num = (label: string, v: string, opts: { int?: boolean; min?: number; max?: number; gt?: number }) => {
    const n = Number(v);
    if (v.trim() === '' || !Number.isFinite(n)) {
      errors.push(`${label}: enter a number`);
      return 0;
    }
    if (opts.int && !Number.isInteger(n)) errors.push(`${label}: whole number required`);
    if (opts.min !== undefined && n < opts.min) errors.push(`${label}: minimum ${opts.min}`);
    if (opts.gt !== undefined && n <= opts.gt) errors.push(`${label}: must be > ${opts.gt}`);
    if (opts.max !== undefined && n > opts.max) errors.push(`${label}: maximum ${opts.max}`);
    return n;
  };
  let mock: Record<string, unknown> = {};
  if (f.target === 'synthetic' && f.mock.trim()) {
    try {
      const m = JSON.parse(f.mock);
      if (!m || typeof m !== 'object' || Array.isArray(m)) errors.push('Mock faults must be a JSON object');
      else mock = m as Record<string, unknown>;
    } catch (e) {
      errors.push(`Mock faults: invalid JSON (${e instanceof Error ? e.message : String(e)})`);
    }
  }
  const body: JobCreate = {
    name: f.name.trim(),
    target: f.target,
    method: f.method,
    worker_count: num('Workers', f.worker_count, { int: true, min: 1, max: 16 }),
    requests_per_minute: num('Requests per minute', f.requests_per_minute, { gt: 0, max: 600 }),
    max_captures: num('Max captures', f.max_captures, { int: true, min: 1 }),
    max_duration_minutes: num('Max duration', f.max_duration_minutes, { gt: 0 }),
    model_budget_usd: num('Model budget', f.model_budget_usd, { min: 0 }),
    max_attempts: num('Max attempts', f.max_attempts, { int: true, min: 1, max: 10 }),
    mock,
  };
  return { body: errors.length ? null : body, errors };
}

export function RunTab({ project, status, active, planId, jobId, setJobId, goTab }: TabProps) {
  const pid = project.id;
  const plans = useResource(`plans:${pid}`, () => api.get<Plan[]>(`/api/projects/${pid}/plans`), { interval: 15000, enabled: active });
  const jobs = useResource(`jobs:${pid}`, () => api.get<Job[]>(`/api/projects/${pid}/jobs`), { interval: 5000, enabled: active });
  const approved = useMemo(() => (plans.data ?? []).filter((p) => p.status === 'approved'), [plans.data]);

  const [planSel, setPlanSel] = useState<string>('');
  useEffect(() => {
    if (planSel && approved.some((p) => p.id === planSel)) return;
    const pref = approved.find((p) => p.id === planId) ?? approved[0];
    setPlanSel(pref ? pref.id : '');
  }, [approved, planId, planSel]);

  // Default to the newest job.
  useEffect(() => {
    if (!jobId && jobs.data && jobs.data.length > 0) setJobId(jobs.data[0].id);
  }, [jobId, jobs.data, setJobId]);

  const [form, setForm] = useState<JobForm>(DEFAULT_FORM);
  const set = <K extends keyof JobForm>(k: K, v: JobForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const [check, setCheck] = useState<{ ok: boolean; errors: string[] } | null>(null);
  const act = useAction();
  const lim = project.effective_limits;

  useEffect(() => setCheck(null), [form, planSel]);

  const validate = async () => {
    const { body, errors } = buildJob(form);
    if (!body) {
      act.setError(errors);
      return;
    }
    if (!planSel) {
      act.setError(['Select an approved plan']);
      return;
    }
    const r = await act.run(() => api.post<{ ok: boolean; errors: string[] }>(`/api/jobs/validate${qs({ plan_id: planSel })}`, body));
    if (r) setCheck(r);
  };

  const create = async () => {
    const { body, errors } = buildJob(form);
    if (!body) {
      act.setError(errors);
      return;
    }
    if (!planSel) {
      act.setError(['Select an approved plan']);
      return;
    }
    const j = await act.run(() => api.post<Job>(`/api/plans/${planSel}/jobs`, body));
    if (j) {
      setJobId(j.id);
      setCheck(null);
      await jobs.reload();
    }
  };

  const planName = (id: string) => plans.data?.find((p) => p.id === id)?.name ?? shortId(id);

  return (
    <div className="run-layout">
      <div className="side side-left">
        <Section title="New job">
          {plans.error && <ErrorList errors={plans.error} />}
          {plans.data && approved.length === 0 && (
            <Notice tone="warn">
              No approved plan yet.{' '}
              <button type="button" className="btn-link" onClick={() => goTab('plan')}>
                Approve a plan in the Plan tab
              </button>{' '}
              first.
            </Notice>
          )}
          <Field label="Approved plan" htmlFor="job-plan">
            <select id="job-plan" value={planSel} onChange={(e) => setPlanSel(e.target.value)} data-testid="job-plan">
              {approved.length === 0 && <option value="">—</option>}
              {approved.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {p.summary?.samples ?? '?'} samples · {p.summary?.planned_views ?? '?'} views
                </option>
              ))}
            </select>
          </Field>
          <Field label="Job name (optional)" htmlFor="job-name">
            <input id="job-name" value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="defaults to method on target" data-testid="job-name" />
          </Field>
          <div className="grid-2">
            <Field label="Target" htmlFor="job-target">
              <select id="job-target" value={form.target} onChange={(e) => set('target', e.target.value as JobCreate['target'])} data-testid="job-target">
                {TARGETS.map((t) => (
                  <option key={t} value={t} disabled={lim ? !lim.permitted_targets.includes(t) : false}>
                    {t}
                    {lim && !lim.permitted_targets.includes(t) ? ' (not permitted)' : ''}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Method" htmlFor="job-method">
              <select id="job-method" value={form.method} onChange={(e) => set('method', e.target.value as JobCreate['method'])} data-testid="job-method">
                {METHODS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                    {lim && !lim.permitted_access_methods.includes(m) ? ' (not permitted)' : ''}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Workers" htmlFor="job-workers" hint={lim ? `limit ${lim.max_workers}` : undefined}>
              <input id="job-workers" type="number" min={1} max={16} step={1} value={form.worker_count} onChange={(e) => set('worker_count', e.target.value)} data-testid="job-workers" />
            </Field>
            <Field
              label="Requests / minute"
              htmlFor="job-rate"
              hint={lim ? (form.target === 'synthetic' ? 'synthetic site ceiling 600/min' : `approval limit ${fmtNum(lim.max_requests_per_minute, 1)}/min`) : undefined}
            >
              <input id="job-rate" type="number" min={0.1} max={600} step={0.5} value={form.requests_per_minute} onChange={(e) => set('requests_per_minute', e.target.value)} data-testid="job-rate" />
            </Field>
            <Field label="Max captures" htmlFor="job-maxcap">
              <input id="job-maxcap" type="number" min={1} step={1} value={form.max_captures} onChange={(e) => set('max_captures', e.target.value)} data-testid="job-max-captures" />
            </Field>
            <Field label="Max duration (min)" htmlFor="job-maxdur">
              <input id="job-maxdur" type="number" min={1} step={1} value={form.max_duration_minutes} onChange={(e) => set('max_duration_minutes', e.target.value)} data-testid="job-max-duration" />
            </Field>
            <Field label="Model budget (USD)" htmlFor="job-budget" hint="required > 0 for openai_agent">
              <input id="job-budget" type="number" min={0} step={0.5} value={form.model_budget_usd} onChange={(e) => set('model_budget_usd', e.target.value)} data-testid="job-budget" />
            </Field>
            <Field label="Max attempts / task" htmlFor="job-attempts">
              <input id="job-attempts" type="number" min={1} max={10} step={1} value={form.max_attempts} onChange={(e) => set('max_attempts', e.target.value)} data-testid="job-max-attempts" />
            </Field>
          </div>
          {form.target === 'synthetic' && (
            <Field
              label="Mock faults (synthetic only, JSON)"
              htmlFor="job-mock"
              hint={
                <>
                  e.g. <code>{'{"fault_by_ordinal": {"3": "captcha", "5": "blank"}}'}</code>
                </>
              }
            >
              <textarea id="job-mock" rows={3} className="mono" value={form.mock} onChange={(e) => set('mock', e.target.value)} spellCheck={false} data-testid="job-mock" />
            </Field>
          )}
          {form.method === 'openai_agent' && (
            <Notice tone="warn">
              OpenAI computer use needs screenshots. It is refused unless cloud image processing is permitted for this project and by the operator
              {status?.processing_mode?.allow_cloud_image_processing ? '' : ' (currently LOCAL ONLY)'}.
            </Notice>
          )}
          <div className="row gap">
            <button type="button" className="btn" onClick={() => void validate()} disabled={act.busy} data-testid="job-validate">
              Check permissions
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void create()} disabled={act.busy || !planSel} data-testid="create-job">
              {act.busy ? 'Working…' : 'Create job'}
            </button>
          </div>
          <ErrorList errors={act.error} title="Refused" />
          {check && check.ok && (
            <Notice tone="ok" testid="job-validate-result">
              All permission checks passed for this plan.
            </Notice>
          )}
          {check && !check.ok && <ErrorList errors={check.errors} title="Permission checks failed" />}
          {lim && (
            <p className="hint">
              Permitted: methods {lim.permitted_access_methods.join(', ') || 'none'}; targets {lim.permitted_targets.join(', ') || 'none'}; max {lim.max_workers} workers;{' '}
              {fmtNum(lim.max_requests_per_minute, 1)} req/min on live targets.
            </p>
          )}
        </Section>

        <Section title="Jobs" actions={<span className="muted">{jobs.data?.length ?? 0}</span>}>
          {jobs.error && <ErrorList errors={jobs.error} />}
          {!jobs.data && !jobs.error && <Loading what="jobs" />}
          {jobs.data && jobs.data.length === 0 && <Empty>No jobs yet.</Empty>}
          {jobs.data && jobs.data.length > 0 && (
            <ul className="list" data-testid="jobs-list">
              {jobs.data.map((j) => {
                const tc = j.task_counts ?? {};
                const total = Object.values(tc).reduce((a, b) => a + b, 0);
                return (
                  <li key={j.id}>
                    <button type="button" className={`list-item ${j.id === jobId ? 'list-item-active' : ''}`} onClick={() => setJobId(j.id)} data-testid="job-list-item">
                      <span className="list-title">{j.name || shortId(j.id)}</span>
                      <Chip value={j.status} />
                      <span className="list-meta">
                        {j.target} · {j.method} · {total} tasks · {j.captures_current} captures · {planName(j.plan_id)} · {fmtTime(j.created_at)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Section>
      </div>
      <div className="run-main">
        {jobId ? <JobPanel key={jobId} jobId={jobId} active={active} onChanged={() => void jobs.reload()} /> : <Empty>Select or create a job.</Empty>}
      </div>
    </div>
  );
}
