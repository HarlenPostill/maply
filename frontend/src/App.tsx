import { useCallback, useEffect, useState } from 'react';
import { api, type Geo, type JobSummary, type Status, type FC } from './api';
import MapView, { type Cone, type DrawMode } from './MapView';
import NewJob from './NewJob';
import ModelSettings from './ModelSettings';
import JobView from './JobView';

export type MapState = {
  area: Geo | null;
  streets: FC | null;
  cones: Cone[];
  drawMode: DrawMode;
  onDrawn?: (g: Geo, mode: 'area' | 'street') => void;
  onConeClick?: (id: string) => void;
  onViewChange?: (bbox: [number, number, number, number], zoom: number) => void;
  selectedId?: string | null;
  fitKey?: string;
  focusBounds?: [number, number, number, number] | null;
  focusKey?: number;
  taggingSegments?: FC | null;
  selectedSegment?: string | null;
  onSegmentClick?: (key: string) => void;
};

const EMPTY_MAP: MapState = { area: null, streets: null, cones: [], drawMode: null };

type View = { kind: 'home' } | { kind: 'new' } | { kind: 'job'; id: string };

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [view, setView] = useState<View>({ kind: 'home' });
  const [mapState, setMapState] = useState<MapState>(EMPTY_MAP);
  const [stageOverlay, setStageOverlay] = useState<React.ReactNode>(null);

  const loadJobs = useCallback(() => api.get<JobSummary[]>('/api/jobs').then(setJobs).catch((e) => setError(e.message)), []);

  useEffect(() => {
    api.get<Status>('/api/status').then(setStatus).catch(() => setError('maply’s server isn’t responding. Start it with docker compose up, then reload.'));
    loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    if (view.kind === 'home') {
      setMapState(EMPTY_MAP);
      setStageOverlay(null);
    }
  }, [view]);

  if (!status) {
    return <div className="boot">{error || 'Loading…'}</div>;
  }

  return (
    <div className="app">
      <aside className="panel">
        <header className="brand">
          <button type="button" className="wordmark" onClick={() => { setView({ kind: 'home' }); loadJobs(); }}>maply</button>
          <span className="brand-note">Street imagery & accessibility</span>
        </header>
        <ModelSettings />

        {view.kind === 'home' && (
          <Home jobs={jobs} onNew={() => setView({ kind: 'new' })} onOpen={(id) => setView({ kind: 'job', id })} />
        )}
        {view.kind === 'new' && (
          <NewJob
            status={status}
            setMap={setMapState}
            onCancel={() => setView({ kind: 'home' })}
            onCreated={(id) => { loadJobs(); setView({ kind: 'job', id }); }}
          />
        )}
        {view.kind === 'job' && (
          <JobView
            key={view.id}
            id={view.id}
            status={status}
            setMap={setMapState}
            setOverlay={setStageOverlay}
            onBack={() => { loadJobs(); setView({ kind: 'home' }); }}
            onDeleted={() => { loadJobs(); setView({ kind: 'home' }); }}
          />
        )}
      </aside>

      <main className="stage">
        <MapView status={status} {...mapState} />
        {stageOverlay}
      </main>
    </div>
  );
}

function Home({ jobs, onNew, onOpen }: { jobs: JobSummary[]; onNew: () => void; onOpen: (id: string) => void }) {
  return (
    <div className="panel-body">
      <div className="home-head">
        <h1>Jobs</h1>
        <button type="button" className="btn btn-primary" onClick={onNew} data-testid="new-job">New job</button>
      </div>
      {jobs.length === 0 ? (
        <div className="empty">
          <p>No jobs yet.</p>
          <p className="muted">Start a job by blocking out a few streets on the map. maply plans every photo and writes the prompt you give to Codex.</p>
        </div>
      ) : (
        <ul className="job-list">
          {jobs.map((j) => {
            const saved = j.counts.done;
            return (
              <li key={j.id}>
                <button type="button" className="job-row" onClick={() => onOpen(j.id)} data-testid="job-row">
                  <span className="job-row-name">{j.name}</span>
                  <span className="job-row-meta">{saved} of {j.counts.total} photos</span>
                  <span className="meter" aria-hidden="true"><span style={{ width: `${j.counts.total ? (100 * saved) / j.counts.total : 0}%` }} /></span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
