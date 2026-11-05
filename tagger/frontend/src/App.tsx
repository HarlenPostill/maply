import { lazy, Suspense, useEffect, useState } from 'react';
import { api, type Geo, type JobSummary, type Status, type FC } from './api';
import type { Cone, DrawMode } from './MapView';
import ModelSettings from './ModelSettings';
import ReviewWorkspace from './ReviewWorkspace';
const Visualiser = lazy(() => import('./visualiser/Visualiser'));

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

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [error, setError] = useState('');
  const [view, setView] = useState<'review' | 'atlas'>('review');
  const [selectedJob, setSelectedJob] = useState('');
  useEffect(() => {
    let active = true;
    Promise.all([api.get<Status>('/api/status'), api.get<JobSummary[]>('/api/jobs')])
      .then(([nextStatus, nextJobs]) => { if (active) { setStatus(nextStatus); setJobs(nextJobs); } })
      .catch((failure: unknown) => { if (active) setError(failure instanceof Error ? failure.message : 'The local Maply server is unavailable.'); });
    return () => { active = false; };
  }, []);
  if (error) return <div className="boot"><div><h1>Maply could not load</h1><p>{error}</p><button className="btn" onClick={() => window.location.reload()}>Try again</button></div></div>;
  if (!status) return <div className="boot">Loading your local dataset…</div>;
  if (view === 'atlas') return <Suspense fallback={<div className="boot">Loading accessibility atlas…</div>}><Visualiser jobs={jobs} status={status} onBack={() => setView('review')} /></Suspense>;
  const job = jobs.find((item) => item.id === selectedJob) ?? jobs[0];
  return <div className="review-app">
    <header className="review-app-header">
      <div className="review-brand"><button className="wordmark" onClick={() => setView('review')}>maply<span> / </span>review</button><span className="review-local">Local dataset</span></div>
      <nav aria-label="Workspace"><button className="review-nav-active" aria-current="page">Review workspace</button><button onClick={() => setView('atlas')}>Accessibility atlas ↗</button></nav>
      <div className="review-settings"><ModelSettings /></div>
    </header>
    {jobs.length > 1 && <div className="review-job-select"><label>Dataset <select value={job?.id ?? ''} onChange={(event) => setSelectedJob(event.target.value)}>{jobs.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label></div>}
    {job ? <ReviewWorkspace key={job.id} job={job} /> : <main className="review-empty-dataset"><h1>No local dataset found</h1><p>Start Maply with the database containing your completed capture job.</p></main>}
  </div>;
}
