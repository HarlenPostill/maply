import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { api } from './lib/api';
import { useAction, useResource, useStoredState } from './lib/hooks';
import type { Project, SystemStatus } from './lib/types';
import { fmtBytes } from './lib/format';
import { ErrorList, Field, Loading, Notice } from './components/common';
import { PlanTab } from './tabs/PlanTab';
import { RunTab } from './tabs/RunTab';
import { CoverageTab } from './tabs/CoverageTab';
import { GalleryTab } from './tabs/GalleryTab';
import { ExportTab } from './tabs/ExportTab';
import { SettingsTab } from './tabs/SettingsTab';

export type TabId = 'plan' | 'run' | 'coverage' | 'gallery' | 'export' | 'settings';
const TABS: { id: TabId; label: string }[] = [
  { id: 'plan', label: 'Plan' },
  { id: 'run', label: 'Run' },
  { id: 'coverage', label: 'Coverage' },
  { id: 'gallery', label: 'Gallery' },
  { id: 'export', label: 'Export' },
  { id: 'settings', label: 'Settings' },
];

export interface TabProps {
  project: Project;
  status: SystemStatus | undefined;
  active: boolean;
  refreshProject: () => Promise<unknown>;
  planId: string | null;
  setPlanId: (id: string | null) => void;
  jobId: string | null;
  setJobId: (id: string | null) => void;
  goTab: (t: TabId) => void;
}

function tabFromHash(): TabId | null {
  const h = window.location.hash.replace(/^#\/?/, '');
  return TABS.some((t) => t.id === h) ? (h as TabId) : null;
}

export function App() {
  const status = useResource('status', () => api.get<SystemStatus>('/api/system/status'), { interval: 10000 });
  const projects = useResource('projects', () => api.get<Project[]>('/api/projects'));
  const [storedPid, setStoredPid] = useStoredState<string | null>('maply.projectId', null);
  const [showCreate, setShowCreate] = useState(false);

  const pid = useMemo(() => {
    const list = projects.data;
    if (!list || list.length === 0) return null;
    return list.find((p) => p.id === storedPid)?.id ?? list[0].id;
  }, [projects.data, storedPid]);

  const project = useResource(pid ? `project:${pid}` : null, () => api.get<Project>(`/api/projects/${pid}`));

  const [tab, setTabState] = useState<TabId>(() => tabFromHash() ?? 'plan');
  const [visited, setVisited] = useState<Set<TabId>>(() => new Set([tabFromHash() ?? 'plan']));
  const goTab = useCallback((t: TabId) => {
    setTabState(t);
    setVisited((v) => (v.has(t) ? v : new Set(v).add(t)));
    if (window.location.hash !== `#${t}`) window.history.replaceState(null, '', `#${t}`);
  }, []);
  useEffect(() => {
    const onHash = () => {
      const t = tabFromHash();
      if (t) goTab(t);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [goTab]);

  // Cross-tab selections, reset when the project changes.
  const [planId, setPlanId] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  useEffect(() => {
    setPlanId(null);
    setJobId(null);
  }, [pid]);

  const reloadProject = project.reload;
  const reloadProjects = projects.reload;
  const refreshProject = useCallback(async () => {
    await Promise.all([reloadProject(), reloadProjects()]);
  }, [reloadProject, reloadProjects]);

  const noProjects = projects.data !== undefined && projects.data.length === 0;

  return (
    <div className="app">
      <Header
        status={status.data}
        statusError={status.error}
        projects={projects.data}
        pid={pid}
        onSelect={(id) => setStoredPid(id)}
        onNew={() => setShowCreate(true)}
      />
      {!noProjects && pid && (
        <nav className="tabs" role="tablist" aria-label="Sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={`tab ${tab === t.id ? 'tab-active' : ''}`}
              data-testid={`tab-${t.id}`}
              onClick={() => goTab(t.id)}
            >
              {t.label}
            </button>
          ))}
          <span className="tabs-spacer" />
          {project.data?.config?.approval_reference && (
            <span className="tabs-meta" title="Research approval reference for this project">
              Approval <strong>{project.data.config.approval_reference}</strong>
              {project.data.config.approval_expires_on ? ` · expires ${project.data.config.approval_expires_on}` : ''}
            </span>
          )}
        </nav>
      )}
      <main className="main">
        {projects.error && <ErrorList errors={projects.error} title="Could not load projects" />}
        {projects.data === undefined && !projects.error && <Loading what="projects" />}
        {(noProjects || showCreate) && (
          <CreateProject
            first={noProjects}
            onCancel={noProjects ? undefined : () => setShowCreate(false)}
            onCreated={async (p) => {
              setStoredPid(p.id);
              setShowCreate(false);
              await projects.reload();
              goTab('plan');
            }}
          />
        )}
        {!noProjects && pid && project.error && <ErrorList errors={project.error} title="Could not load project" />}
        {!noProjects && pid && project.data && project.data.id === pid && (
          <div key={pid} className="tab-host">
            {TABS.filter((t) => visited.has(t.id)).map((t) => {
              const props: TabProps = {
                project: project.data!,
                status: status.data,
                active: tab === t.id,
                refreshProject,
                planId,
                setPlanId,
                jobId,
                setJobId,
                goTab,
              };
              return (
                <div key={t.id} className={`tab-pane tab-pane-${t.id}`} hidden={tab !== t.id} role="tabpanel">
                  {t.id === 'plan' && <PlanTab {...props} />}
                  {t.id === 'run' && <RunTab {...props} />}
                  {t.id === 'coverage' && <CoverageTab {...props} />}
                  {t.id === 'gallery' && <GalleryTab {...props} />}
                  {t.id === 'export' && <ExportTab {...props} />}
                  {t.id === 'settings' && <SettingsTab {...props} />}
                </div>
              );
            })}
          </div>
        )}
      </main>
      <footer className="footer">
        Local research tool — no publication or sharing features. Camera positions are source-reported, not feature locations.
      </footer>
    </div>
  );
}

function Header({
  status,
  statusError,
  projects,
  pid,
  onSelect,
  onNew,
}: {
  status: SystemStatus | undefined;
  statusError: string[] | null;
  projects: Project[] | undefined;
  pid: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  const pm = status?.processing_mode;
  const cloud = !!pm?.allow_cloud_image_processing;
  const bytes = status?.storage?.bytes ?? null;
  const total = bytes ? Object.values(bytes).reduce((a, b) => a + (Number(b) || 0), 0) : null;
  const storageTitle = status
    ? [
        status.storage.note,
        '',
        ...Object.entries(bytes ?? {}).map(([k, v]) => `${k}: ${fmtBytes(v)}`),
        '',
        `projects: ${status.storage.projects_dir}`,
        `exports: ${status.storage.exports_dir}`,
        `browser state: ${status.storage.browser_state_dir}`,
      ].join('\n')
    : '';
  return (
    <header className="header">
      <div className="brand" aria-label="maply">
        maply
      </div>
      <div className="header-project">
        <label htmlFor="project-select" className="sr-only">
          Project
        </label>
        <select
          id="project-select"
          data-testid="project-select"
          value={pid ?? ''}
          onChange={(e) => onSelect(e.target.value)}
          disabled={!projects || projects.length === 0}
        >
          {(!projects || projects.length === 0) && <option value="">No projects</option>}
          {(projects ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onNew} data-testid="new-project">
          + New project
        </button>
      </div>
      <div className="header-spacer" />
      {statusError && (
        <span className="badge badge-bad" title={statusError.join('\n')}>
          API unreachable
        </span>
      )}
      {pm && (
        <span
          className={`mode-badge ${cloud ? 'mode-cloud' : 'mode-local'}`}
          data-testid="processing-mode"
          title={
            cloud
              ? 'Cloud image processing is enabled by the operator. Screenshots may be sent to the configured model provider for permitted projects/jobs.'
              : 'LOCAL ONLY: no screenshot or image-bearing payload is sent to any model provider.' +
                (pm.allow_synthetic_cloud_image_processing ? ' (Synthetic-image egress is enabled for mock-site tests only.)' : '')
          }
        >
          {pm.label}
          {(pm.openai_configured || pm.openai_model) && (
            <span className="mode-extra">
              {pm.openai_model ? ` · ${pm.openai_model}` : ''}
              {!pm.openai_store ? ' · store=false' : ' · store=TRUE'}
            </span>
          )}
          {pm.allow_synthetic_cloud_image_processing && <span className="mode-extra"> · synthetic egress on</span>}
        </span>
      )}
      {status && (
        <span className="header-stat header-stat-storage" title={storageTitle} data-testid="storage-indicator">
          <span className="header-stat-label">Storage</span>
          <code className="mono">{status.storage.host_data_dir ?? status.storage.data_dir}</code>
          <strong>{fmtBytes(total)}</strong>
        </span>
      )}
      {status && (
        <span className={`header-stat ${status.workers_alive > 0 ? 'stat-live' : ''}`} title="Workers with a heartbeat in the last 30 s" data-testid="workers-alive">
          <span className="header-stat-label">Workers alive</span>
          <strong>{status.workers_alive}</strong>
        </span>
      )}
      {status && !status.database?.ok && <span className="badge badge-bad">DB not OK</span>}
    </header>
  );
}

function CreateProject({ first, onCreated, onCancel }: { first: boolean; onCreated: (p: Project) => void | Promise<void>; onCancel?: () => void }) {
  const [name, setName] = useState('');
  const [approvalRef, setApprovalRef] = useState('');
  const [expires, setExpires] = useState('');
  const [description, setDescription] = useState('');
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      act.setError(['Project name is required']);
      return;
    }
    const p = await act.run(() =>
      api.post<Project>('/api/projects', {
        name: name.trim(),
        description,
        config: { approval_reference: approvalRef.trim(), approval_expires_on: expires || null },
      }),
    );
    if (p) await onCreated(p);
  };
  return (
    <div className="create-project">
      <form className="panel" onSubmit={submit} data-testid="create-project-form">
        <header className="panel-head">
          <h2>{first ? 'Create your first project' : 'New project'}</h2>
        </header>
        <div className="panel-body">
          <Notice tone="info">
            A project records the research approval and its operational limits. The approved scope defaults to the server&apos;s configured City of Sydney
            boundary (editable later in Settings).
          </Notice>
          <Field label="Project name" htmlFor="cp-name">
            <input id="cp-name" data-testid="create-project-name" value={name} onChange={(e) => setName(e.target.value)} autoFocus required />
          </Field>
          <Field label="Approval reference" htmlFor="cp-ref" hint="Ethics / research approval identifier">
            <input id="cp-ref" data-testid="create-project-approval" value={approvalRef} onChange={(e) => setApprovalRef(e.target.value)} />
          </Field>
          <Field label="Approval expiry date" htmlFor="cp-exp" hint="Jobs targeting google_maps are refused after this date">
            <input id="cp-exp" type="date" data-testid="create-project-expiry" value={expires} onChange={(e) => setExpires(e.target.value)} />
          </Field>
          <Field label="Description (optional)" htmlFor="cp-desc">
            <textarea id="cp-desc" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <ErrorList errors={act.error} />
          <div className="row gap">
            <button type="submit" className="btn btn-primary" data-testid="create-project-submit" disabled={act.busy}>
              {act.busy ? 'Creating…' : 'Create project'}
            </button>
            {onCancel && (
              <button type="button" className="btn" onClick={onCancel}>
                Cancel
              </button>
            )}
          </div>
        </div>
      </form>
    </div>
  );
}
