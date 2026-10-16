import { useState } from 'react';
import type { TabProps } from '../App';
import { api, qs } from '../lib/api';
import { useAction, useResource } from '../lib/hooks';
import type { ExportRow, Job, RetentionPreview } from '../lib/types';
import { fmtBytes, fmtNum, fmtTime, shortId } from '../lib/format';
import { Copyable, Empty, ErrorList, Field, JobSelect, KV, Loading, Notice, Section } from '../components/common';

export function ExportTab({ project, active }: TabProps) {
  const pid = project.id;
  const jobs = useResource(`jobs:${pid}`, () => api.get<Job[]>(`/api/projects/${pid}/jobs`), { interval: 15000, enabled: active });
  const exports = useResource(`exports:${pid}`, () => api.get<ExportRow[]>(`/api/projects/${pid}/exports`));
  const retention = useResource(`retention:${pid}`, () => api.get<RetentionPreview>(`/api/projects/${pid}/retention`), { interval: 30000, enabled: active });

  const [jobId, setJobId] = useState<string | null>(null);
  const [include, setInclude] = useState<'verified' | 'verified_and_review' | 'all_current'>('verified_and_review');
  const [copyMode, setCopyMode] = useState<'hardlink' | 'copy'>('hardlink');
  const act = useAction();
  const [created, setCreated] = useState<ExportRow | null>(null);

  const create = async () => {
    setCreated(null);
    const r = await act.run(() => api.post<ExportRow>(`/api/projects/${pid}/exports`, { job_id: jobId, include, copy_mode: copyMode }));
    if (r) {
      setCreated(r);
      await exports.reload();
    }
  };

  const [confirmName, setConfirmName] = useState('');
  const ret = useAction();
  const [retResult, setRetResult] = useState<{ cutoff: string; observations_deleted: number; exports_deleted: number } | null>(null);
  const applyRetention = async () => {
    if (confirmName !== project.name) return;
    const p = retention.data;
    if (
      !window.confirm(
        `PERMANENTLY DELETE research data for "${project.name}"?\n\n` +
          `${p ? `${p.observations} observation(s), ${fmtBytes(p.bytes)}, collected before ${fmtTime(p.cutoff)}` : 'All data older than the retention cutoff'}, ` +
          'plus local export packages older than the cutoff, will be removed from disk.\n\nThis cannot be undone.',
      )
    )
      return;
    const r = await ret.run(() => api.post<{ cutoff: string; observations_deleted: number; exports_deleted: number }>(`/api/projects/${pid}/retention/apply${qs({ confirm: confirmName })}`));
    if (r) {
      setRetResult(r);
      setConfirmName('');
      await Promise.all([retention.reload(), exports.reload()]);
    }
  };

  return (
    <div className="page-layout">
      <div className="page-col">
        <Section title="Create local package">
          <Notice tone="info">
            Packages are written to the local data directory for downstream research tools. This application has no upload, publish or share function.
          </Notice>
          <div className="grid-3">
            <Field label="Job" htmlFor="export-job">
              <JobSelect jobs={jobs.data} value={jobId} onChange={setJobId} allowNone noneLabel="All jobs in project" testid="export-job" />
            </Field>
            <Field label="Include" htmlFor="export-include">
              <select id="export-include" value={include} onChange={(e) => setInclude(e.target.value as typeof include)} data-testid="export-include">
                <option value="verified">verified only</option>
                <option value="verified_and_review">verified + needs review</option>
                <option value="all_current">all current observations</option>
              </select>
            </Field>
            <Field label="File mode" htmlFor="export-copy" hint="hardlink saves disk space (falls back to copy)">
              <select id="export-copy" value={copyMode} onChange={(e) => setCopyMode(e.target.value as typeof copyMode)} data-testid="export-copy-mode">
                <option value="hardlink">hardlink</option>
                <option value="copy">copy</option>
              </select>
            </Field>
          </div>
          <button type="button" className="btn btn-primary" onClick={() => void create()} disabled={act.busy} data-testid="export-create">
            {act.busy ? 'Writing package…' : 'Create local package'}
          </button>
          <ErrorList errors={act.error} />
          {created && (
            <Notice tone="ok" testid="export-result">
              Package written: {fmtNum(created.observation_count)} observations
              <br />
              <Copyable text={created.absolute_path} />
              <br />
              manifest sha256 <Copyable text={created.manifest_sha256} />
            </Notice>
          )}
        </Section>

        <Section title="Local packages" actions={<span className="muted">{exports.data?.length ?? 0}</span>}>
          {exports.error && <ErrorList errors={exports.error} />}
          {!exports.data && !exports.error && <Loading what="exports" />}
          {exports.data && exports.data.length === 0 && <Empty>No packages yet.</Empty>}
          {exports.data && exports.data.length > 0 && (
            <div className="table-scroll">
              <table className="table table-compact" data-testid="exports-table">
                <thead>
                  <tr>
                    <th>Created</th>
                    <th>Job</th>
                    <th className="right">Observations</th>
                    <th>Absolute path</th>
                    <th>Manifest SHA-256</th>
                  </tr>
                </thead>
                <tbody>
                  {exports.data.map((e) => (
                    <tr key={e.id}>
                      <td className="nowrap">{fmtTime(e.created_at)}</td>
                      <td>{e.job_id ? shortId(e.job_id) : 'all'}</td>
                      <td className="right">{fmtNum(e.observation_count)}</td>
                      <td>
                        <Copyable text={e.absolute_path} />
                      </td>
                      <td>
                        <Copyable text={e.manifest_sha256} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      </div>

      <div className="page-col page-col-narrow">
        <Section title="Retention" className="panel-danger">
          {retention.error && <ErrorList errors={retention.error} />}
          {!retention.data && !retention.error && <Loading what="retention preview" />}
          {retention.data && (
            <KV
              rows={[
                ['Retention period', `${retention.data.retention_days} days`],
                ['Cutoff', fmtTime(retention.data.cutoff)],
                ['Observations past cutoff', fmtNum(retention.data.observations)],
                ['Bytes to delete', fmtBytes(retention.data.bytes)],
              ]}
            />
          )}
          <Notice tone="bad">
            Applying retention permanently deletes images and metadata collected before the cutoff, plus older local export packages. There is no undo.
          </Notice>
          <Field label={<>Type the project name <strong>{project.name}</strong> to enable</>} htmlFor="retention-confirm">
            <input id="retention-confirm" value={confirmName} onChange={(e) => setConfirmName(e.target.value)} autoComplete="off" data-testid="retention-confirm" />
          </Field>
          <button type="button" className="btn btn-danger" disabled={confirmName !== project.name || ret.busy} onClick={() => void applyRetention()} data-testid="retention-apply">
            {ret.busy ? 'Deleting…' : 'Apply retention (delete data)'}
          </button>
          <ErrorList errors={ret.error} />
          {retResult && (
            <Notice tone="ok">
              Retention applied (cutoff {fmtTime(retResult.cutoff)}): {retResult.observations_deleted} observation(s) and {retResult.exports_deleted} export package(s) deleted.
            </Notice>
          )}
        </Section>
      </div>
    </div>
  );
}
