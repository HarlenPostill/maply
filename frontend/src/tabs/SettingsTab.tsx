import { useEffect, useState } from 'react';
import type { TabProps } from '../App';
import { api } from '../lib/api';
import { useAction, useResource } from '../lib/hooks';
import type { EgressRow, Project, ProjectConfig } from '../lib/types';
import { fmtNum, fmtTime, shortId } from '../lib/format';
import { Chip, Empty, ErrorList, Field, JsonBlock, KV, Loading, Mono, Notice, Section } from '../components/common';

const ALL_METHODS = ['deterministic_browser', 'manual', 'openai_agent', 'streetview_static_api'];
const ALL_TARGETS = ['synthetic', 'google_maps'];

interface ConfigForm {
  purpose: string;
  approval_reference: string;
  approval_expires_on: string;
  permitted_access_methods: string[];
  permitted_targets: string[];
  allow_third_party_processing: boolean;
  third_party_processors: string[];
  max_captures_total: string;
  max_workers: string;
  max_requests_per_minute: string;
  retention_days: string;
  scope_buffer_m: string;
}

function toForm(c: Partial<ProjectConfig> | null | undefined): ConfigForm {
  return {
    purpose: c?.purpose ?? '',
    approval_reference: c?.approval_reference ?? '',
    approval_expires_on: c?.approval_expires_on ?? '',
    permitted_access_methods: c?.permitted_access_methods ?? ['deterministic_browser', 'manual', 'openai_agent'],
    permitted_targets: c?.permitted_targets ?? ['synthetic', 'google_maps'],
    allow_third_party_processing: !!c?.allow_third_party_processing,
    third_party_processors: c?.third_party_processors ?? [],
    max_captures_total: String(c?.max_captures_total ?? 2000),
    max_workers: String(c?.max_workers ?? 2),
    max_requests_per_minute: String(c?.max_requests_per_minute ?? 10),
    retention_days: String(c?.retention_days ?? 365),
    scope_buffer_m: String(c?.scope_buffer_m ?? 0),
  };
}

function fromForm(f: ConfigForm): { config: ProjectConfig | null; errors: string[] } {
  const errors: string[] = [];
  const n = (label: string, v: string, o: { int?: boolean; min?: number; max?: number; gt?: number }) => {
    const x = Number(v);
    if (v.trim() === '' || !Number.isFinite(x)) errors.push(`${label}: enter a number`);
    else {
      if (o.int && !Number.isInteger(x)) errors.push(`${label}: whole number required`);
      if (o.min !== undefined && x < o.min) errors.push(`${label}: minimum ${o.min}`);
      if (o.max !== undefined && x > o.max) errors.push(`${label}: maximum ${o.max}`);
      if (o.gt !== undefined && x <= o.gt) errors.push(`${label}: must be > ${o.gt}`);
    }
    return x;
  };
  const config: ProjectConfig = {
    purpose: f.purpose,
    approval_reference: f.approval_reference.trim(),
    approval_expires_on: f.approval_expires_on || null,
    permitted_access_methods: f.permitted_access_methods,
    permitted_targets: f.permitted_targets,
    allow_third_party_processing: f.allow_third_party_processing,
    third_party_processors: f.third_party_processors,
    max_captures_total: n('Max captures total', f.max_captures_total, { int: true, min: 1 }),
    max_workers: n('Max workers', f.max_workers, { int: true, min: 1, max: 16 }),
    max_requests_per_minute: n('Max requests/min', f.max_requests_per_minute, { gt: 0 }),
    retention_days: n('Retention days', f.retention_days, { int: true, min: 1 }),
    scope_buffer_m: n('Scope buffer', f.scope_buffer_m, { min: 0, max: 200 }),
  };
  return { config: errors.length ? null : config, errors };
}

function toggle(list: string[], v: string, on: boolean): string[] {
  return on ? [...new Set([...list, v])] : list.filter((x) => x !== v);
}

export function SettingsTab({ project, status, active, refreshProject }: TabProps) {
  const pid = project.id;
  const [form, setForm] = useState<ConfigForm>(() => toForm(project.config));
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? '');
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (!dirty) {
      setForm(toForm(project.config));
      setName(project.name);
      setDescription(project.description ?? '');
    }
  }, [project, dirty]);
  const upd = <K extends keyof ConfigForm>(k: K, v: ConfigForm[K]) => {
    setForm((f) => ({ ...f, [k]: v }));
    setDirty(true);
  };
  const act = useAction();
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setSaved(false);
    const { config, errors } = fromForm(form);
    if (!config) {
      act.setError(errors);
      return;
    }
    if (!name.trim()) {
      act.setError(['Project name is required']);
      return;
    }
    const r = await act.run(() => api.patch<Project>(`/api/projects/${pid}`, { name: name.trim(), description, config }));
    if (r) {
      setDirty(false);
      setSaved(true);
      await refreshProject();
    }
  };

  // Scope upload
  const [scopeText, setScopeText] = useState('');
  const scopeAct = useAction();
  const [scopeSaved, setScopeSaved] = useState(false);
  const saveScope = async () => {
    setScopeSaved(false);
    let obj: unknown;
    try {
      obj = JSON.parse(scopeText);
    } catch (e) {
      scopeAct.setError([`Not valid JSON: ${e instanceof Error ? e.message : String(e)}`]);
      return;
    }
    const r = await scopeAct.run(() => api.patch<Project>(`/api/projects/${pid}`, { scope_geojson: obj }));
    if (r) {
      setScopeText('');
      setScopeSaved(true);
      await refreshProject();
    }
  };

  const egress = useResource('egress', () => api.get<EgressRow[]>('/api/egress?limit=200'), { interval: 15000, enabled: active });
  const lim = project.effective_limits;
  const pm = status?.processing_mode;

  return (
    <div className="page-layout">
      <div className="page-col">
        <Section
          title="Project configuration"
          actions={
            <>
              {dirty && <Chip tone="warn">unsaved</Chip>}
              <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={act.busy || !dirty} data-testid="settings-save">
                {act.busy ? 'Saving…' : 'Save configuration'}
              </button>
            </>
          }
        >
          <p className="hint">Project settings can only narrow the operator-level limits set in the server environment.</p>
          <div className="grid-2">
            <Field label="Project name" htmlFor="cfg-name">
              <input
                id="cfg-name"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setDirty(true);
                }}
              />
            </Field>
            <Field label="Description" htmlFor="cfg-desc">
              <input
                id="cfg-desc"
                value={description}
                onChange={(e) => {
                  setDescription(e.target.value);
                  setDirty(true);
                }}
              />
            </Field>
          </div>
          <Field label="Purpose" htmlFor="cfg-purpose" hint="Written into every export package's restrictions notice">
            <textarea id="cfg-purpose" rows={3} value={form.purpose} onChange={(e) => upd('purpose', e.target.value)} />
          </Field>
          <div className="grid-2">
            <Field label="Approval reference" htmlFor="cfg-ref">
              <input id="cfg-ref" value={form.approval_reference} onChange={(e) => upd('approval_reference', e.target.value)} />
            </Field>
            <Field label="Approval expires on" htmlFor="cfg-exp" hint="google_maps jobs are refused after this date">
              <input id="cfg-exp" type="date" value={form.approval_expires_on} onChange={(e) => upd('approval_expires_on', e.target.value)} />
            </Field>
          </div>
          <fieldset className="params-group">
            <legend>Permitted access methods</legend>
            <div className="check-row">
              {ALL_METHODS.map((m) => (
                <label key={m} className="check">
                  <input type="checkbox" checked={form.permitted_access_methods.includes(m)} onChange={(e) => upd('permitted_access_methods', toggle(form.permitted_access_methods, m, e.target.checked))} />
                  {m}
                  {status && !status.operator_limits.permitted_access_methods.includes(m) && <span className="muted small"> (operator disabled)</span>}
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset className="params-group">
            <legend>Permitted targets</legend>
            <div className="check-row">
              {ALL_TARGETS.map((t) => (
                <label key={t} className="check">
                  <input type="checkbox" checked={form.permitted_targets.includes(t)} onChange={(e) => upd('permitted_targets', toggle(form.permitted_targets, t, e.target.checked))} />
                  {t}
                  {status && !status.operator_limits.permitted_targets.includes(t) && <span className="muted small"> (operator disabled)</span>}
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset className="params-group">
            <legend>Third-party (cloud) processing of research imagery</legend>
            <div className="check-row">
              <label className="check">
                <input type="checkbox" checked={form.allow_third_party_processing} onChange={(e) => upd('allow_third_party_processing', e.target.checked)} />
                Project permits third-party processing
              </label>
              <label className="check">
                <input type="checkbox" checked={form.third_party_processors.includes('openai')} onChange={(e) => upd('third_party_processors', toggle(form.third_party_processors, 'openai', e.target.checked))} />
                openai (permitted processor)
              </label>
            </div>
            <p className="hint">Cloud image processing also requires the operator setting ALLOW_CLOUD_IMAGE_PROCESSING; all three conditions must hold.</p>
          </fieldset>
          <div className="grid-3">
            <Field label="Max captures (total)" htmlFor="cfg-maxcap">
              <input id="cfg-maxcap" type="number" min={1} step={1} value={form.max_captures_total} onChange={(e) => upd('max_captures_total', e.target.value)} />
            </Field>
            <Field label="Max workers" htmlFor="cfg-maxw" hint="1–16">
              <input id="cfg-maxw" type="number" min={1} max={16} step={1} value={form.max_workers} onChange={(e) => upd('max_workers', e.target.value)} />
            </Field>
            <Field label="Max requests / min" htmlFor="cfg-rpm">
              <input id="cfg-rpm" type="number" min={0.1} step={0.5} value={form.max_requests_per_minute} onChange={(e) => upd('max_requests_per_minute', e.target.value)} />
            </Field>
            <Field label="Retention (days)" htmlFor="cfg-ret">
              <input id="cfg-ret" type="number" min={1} step={1} value={form.retention_days} onChange={(e) => upd('retention_days', e.target.value)} />
            </Field>
            <Field label="Scope buffer (m)" htmlFor="cfg-buf" hint="0–200">
              <input id="cfg-buf" type="number" min={0} max={200} step={1} value={form.scope_buffer_m} onChange={(e) => upd('scope_buffer_m', e.target.value)} />
            </Field>
          </div>
          <ErrorList errors={act.error} title="Not saved" />
          {saved && <Notice tone="ok">Configuration saved. A new config version is recorded with each plan/job.</Notice>}
          <div className="row gap">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={!dirty}
              onClick={() => {
                setForm(toForm(project.config));
                setName(project.name);
                setDescription(project.description ?? '');
                setDirty(false);
                act.setError(null);
              }}
            >
              Discard changes
            </button>
          </div>
        </Section>

        <Section title="Effective limits" testid="effective-limits">
          {!lim && <Loading what="limits" />}
          {lim && (
            <>
              <KV
                rows={[
                  ['Access methods', lim.permitted_access_methods.join(', ') || 'none'],
                  ['Targets', lim.permitted_targets.join(', ') || 'none'],
                  ['Max workers', lim.max_workers],
                  ['Max requests / min (live targets)', fmtNum(lim.max_requests_per_minute, 1)],
                  ['Max captures (total)', fmtNum(lim.max_captures_total)],
                  ['Retention', `${lim.retention_days} days`],
                  ['Approval expires', lim.approval_expires_on ?? 'not set'],
                  ['Cloud image processing', lim.cloud_image_processing_allowed ? <Chip key="c" tone="warn">allowed</Chip> : <Chip key="c" tone="ok">off</Chip>],
                  ['Synthetic image egress (tests)', lim.synthetic_cloud_image_processing_allowed ? <Chip key="s" tone="warn">allowed</Chip> : <Chip key="s" tone="ok">off</Chip>],
                ]}
              />
              {lim.reasons.length > 0 && (
                <Notice tone="info">
                  <strong>Why cloud image processing is off</strong>
                  <ul>
                    {lim.reasons.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </Notice>
              )}
            </>
          )}
        </Section>

        <Section title="Approved scope">
          <p className="hint">Upload or paste a GeoJSON Polygon / MultiPolygon / Feature / FeatureCollection (WGS84). Planning refuses geometry outside this scope.</p>
          <Field label="GeoJSON file" htmlFor="scope-file">
            <input id="scope-file" type="file" accept=".geojson,.json,application/geo+json,application/json" onChange={async (e) => setScopeText((await e.target.files?.[0]?.text()) ?? '')} />
          </Field>
          <Field label="or paste" htmlFor="scope-text">
            <textarea id="scope-text" rows={4} className="mono" value={scopeText} onChange={(e) => setScopeText(e.target.value)} spellCheck={false} />
          </Field>
          <button type="button" className="btn" onClick={() => void saveScope()} disabled={scopeAct.busy || !scopeText.trim()} data-testid="scope-save">
            Replace scope
          </button>
          <ErrorList errors={scopeAct.error} />
          {scopeSaved && <Notice tone="ok">Scope replaced. Existing plans keep their geometry; new plans are checked against the new scope.</Notice>}
          <JsonBlock value={project.scope} summary="Current scope geometry" />
        </Section>
      </div>

      <div className="page-col">
        <Section title="Processing mode" testid="system-processing">
          {!pm && <Loading what="status" />}
          {pm && (
            <>
              <div className={`mode-banner ${pm.allow_cloud_image_processing ? 'mode-cloud' : 'mode-local'}`}>{pm.label}</div>
              {pm.allow_cloud_image_processing ? (
                <p>
                  Cloud image processing is <strong>enabled by the operator</strong>. For projects that also permit it (and list OpenAI as a processor), openai_agent jobs on
                  google_maps send screenshots to the model provider.
                </p>
              ) : (
                <p>
                  <strong>LOCAL ONLY</strong> means no screenshot or image-bearing payload is sent to any model provider. Captures, verification and storage happen on this
                  machine. Text-only model calls (e.g. a startup model-access check) may still occur if a model is configured.
                </p>
              )}
              <KV
                rows={[
                  ['Synthetic image egress (mock site tests)', pm.allow_synthetic_cloud_image_processing ? 'enabled' : 'disabled'],
                  ['OpenAI configured', pm.openai_configured ? 'yes' : 'no'],
                  ['Model', pm.openai_model ?? null],
                  ['Response storage', pm.openai_store ? 'store=true' : 'store=false (provider asked not to retain responses)'],
                  ['Mock panorama site', status?.mock_panorama_enabled ? 'enabled' : 'disabled'],
                  ['Database', status?.database?.ok ? 'ok' : 'NOT OK'],
                  ['Version', status?.version ?? null],
                ]}
              />
            </>
          )}
        </Section>

        <Section title="Operator limits">
          {status && (
            <KV
              rows={[
                ['Access methods', status.operator_limits.permitted_access_methods.join(', ')],
                ['Targets', status.operator_limits.permitted_targets.join(', ')],
                ['Max workers', status.operator_limits.max_workers],
                ['Max requests / min', fmtNum(status.operator_limits.max_requests_per_minute, 1)],
                ['Max captures / job', fmtNum(status.operator_limits.max_captures_per_job)],
              ]}
            />
          )}
        </Section>

        <Section title="External contacts">
          {status && (
            <table className="table table-compact">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>From</th>
                  <th>Data</th>
                  <th>When / host</th>
                </tr>
              </thead>
              <tbody>
                {status.external_contacts.map((c, i) => (
                  <tr key={i}>
                    <td>{c.service}</td>
                    <td>{c.from ?? '—'}</td>
                    <td>{c.data ?? '—'}</td>
                    <td className="break">{c.when ?? c.host ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        <Section title="System checks">
          {status && status.checks.length === 0 && <Empty>No checks recorded.</Empty>}
          {status && status.checks.length > 0 && (
            <table className="table table-compact">
              <thead>
                <tr>
                  <th>Check</th>
                  <th>Status</th>
                  <th>Detail</th>
                  <th>Checked</th>
                </tr>
              </thead>
              <tbody>
                {status.checks.map((c) => (
                  <tr key={c.name}>
                    <td>
                      <Mono>{c.name}</Mono>
                    </td>
                    <td>
                      <Chip value={c.status} />
                    </td>
                    <td className="mono small break">{typeof c.detail === 'object' && c.detail !== null ? JSON.stringify(c.detail) : String(c.detail ?? '—')}</td>
                    <td className="nowrap">{fmtTime(c.checked_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        <Section title="Egress log" actions={status ? <span className="muted">last 24 h: {status.egress_last_24h.allowed} allowed · {status.egress_last_24h.blocked} blocked</span> : undefined}>
          {egress.error && <ErrorList errors={egress.error} />}
          {!egress.data && !egress.error && <Loading what="egress log" />}
          {egress.data && egress.data.length === 0 && <Empty>No egress events recorded.</Empty>}
          {egress.data && egress.data.length > 0 && (
            <div className="table-scroll table-scroll-tall">
              <table className="table table-compact" data-testid="egress-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Decision</th>
                    <th>Provider</th>
                    <th>Endpoint</th>
                    <th>Data class</th>
                    <th className="right">Images</th>
                    <th>Reason</th>
                    <th>Job</th>
                  </tr>
                </thead>
                <tbody>
                  {egress.data.map((e) => (
                    <tr key={e.id}>
                      <td className="nowrap">{fmtTime(e.created_at)}</td>
                      <td>
                        <Chip value={e.decision} />
                      </td>
                      <td>{e.provider}</td>
                      <td className="mono small break">{e.endpoint ?? '—'}</td>
                      <td>{e.data_class ?? '—'}</td>
                      <td className="right">{e.image_count ?? 0}</td>
                      <td>{e.reason ?? '—'}</td>
                      <td>{e.job_id ? shortId(e.job_id) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}
