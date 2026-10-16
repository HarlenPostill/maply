import { useEffect, useRef } from 'react';
import type { TabProps } from '../App';
import { api } from '../lib/api';
import { useResource } from '../lib/hooks';
import type { Coverage, Job, PlanDetail } from '../lib/types';
import { fmtNum, humanize } from '../lib/format';
import { L, SIDE_COLORS, boundsOfGeometries, escapeHtml, fitTo, geometryLayer, hasBasemap, useLeafletMap } from '../lib/map';
import { CountChips, Empty, ErrorList, JobSelect, KV, Loading, Mono, Progress, Section } from '../components/common';
import { TASK_STATES, TERMINAL_JOB } from '../components/JobPanel';

export const COVERAGE_COLORS: Record<string, string> = {
  verified: '#1f8a3b',
  reused: '#0e8a8a',
  needs_review: '#d18b00',
  missing: '#cc3333',
  no_imagery: '#7b4fb3',
  pending: '#c3c8cf',
  excluded: '#8a94a3',
};
const VIEW_KEYS = ['verified', 'reused', 'needs_review', 'missing', 'no_imagery', 'pending'];

export function CoverageTab({ project, status, active, jobId, setJobId }: TabProps) {
  const pid = project.id;
  const jobs = useResource(`jobs:${pid}`, () => api.get<Job[]>(`/api/projects/${pid}/jobs`), { interval: 10000, enabled: active });
  useEffect(() => {
    if (!jobId && jobs.data && jobs.data.length > 0) setJobId(jobs.data[0].id);
  }, [jobId, jobs.data, setJobId]);
  const job = jobs.data?.find((j) => j.id === jobId);
  const live = !!job && !TERMINAL_JOB.has(job.status);
  const cov = useResource(jobId ? `cov:${jobId}` : null, () => api.get<Coverage>(`/api/jobs/${jobId}/coverage`), { interval: live ? 5000 : null, enabled: active });
  const plan = useResource(job ? `plan:${job.plan_id}` : null, () => api.get<PlanDetail>(`/api/plans/${job!.plan_id}`));

  const mapEl = useRef<HTMLDivElement>(null);
  const map = useLeafletMap(mapEl, status?.basemap);
  const groups = useRef<{ plan: L.LayerGroup; samples: L.LayerGroup; cameras: L.LayerGroup } | null>(null);
  useEffect(() => {
    if (!map) return;
    groups.current = {
      plan: L.layerGroup([], { pmIgnore: true }).addTo(map),
      samples: L.layerGroup([], { pmIgnore: true }).addTo(map),
      cameras: L.layerGroup([], { pmIgnore: true }).addTo(map),
    };
    return () => {
      groups.current = null;
    };
  }, [map]);

  useEffect(() => {
    if (active && map) window.setTimeout(() => map.invalidateSize(), 0);
  }, [active, map]);

  // Plan segments underneath.
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    g.plan.clearLayers();
    const gj = plan.data?.geojson;
    if (!gj) return;
    for (const f of gj.footpaths?.features ?? []) {
      geometryLayer(f.geometry, { color: SIDE_COLORS[f.properties.side] ?? SIDE_COLORS.none, weight: 1.5, opacity: 0.5, dashArray: '3 6', interactive: false, pmIgnore: true })?.addTo(g.plan);
    }
    for (const f of gj.segments?.features ?? []) {
      geometryLayer(f.geometry, { color: '#334155', weight: 8, opacity: 0.25, lineCap: 'butt', pmIgnore: true })
        ?.bindTooltip(`<code>${escapeHtml(f.properties.segment_key)}</code> · ${fmtNum(f.properties.length_m, 1)} m`, { sticky: true })
        .addTo(g.plan);
    }
  }, [map, plan.data]);

  // Samples + camera positions.
  const fitted = useRef<string | null>(null);
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    g.samples.clearLayers();
    g.cameras.clearLayers();
    const c = cov.data;
    if (!c) return;
    for (const f of c.samples?.features ?? []) {
      const p = f.properties;
      const xy = f.geometry.coordinates as number[];
      const color = COVERAGE_COLORS[p.coverage] ?? '#555';
      const excluded = p.coverage === 'excluded';
      const views = Object.entries(p.views ?? {})
        .map(([k, v]) => `${humanize(k)} ${v}`)
        .join(', ');
      L.circleMarker([xy[1], xy[0]], {
        radius: 7,
        color: excluded ? '#8a94a3' : '#ffffff',
        weight: excluded ? 2 : 1.5,
        fillColor: color,
        fillOpacity: excluded ? 0 : 0.95,
        pmIgnore: true,
      })
        .bindTooltip(
          `<strong>#${p.ordinal}</strong> · ${escapeHtml(humanize(p.kind))} · <strong>${escapeHtml(humanize(p.coverage))}</strong><br>` +
            `task: ${escapeHtml(humanize(p.task_status))} · attempts ${p.attempts}<br>` +
            `views: ${escapeHtml(views || '—')}` +
            (p.reason ? `<br><span class="tt-reason">${escapeHtml(p.reason)}</span>` : ''),
          { direction: 'top', offset: [0, -6] },
        )
        .addTo(g.samples);
    }
    for (const f of c.cameras?.features ?? []) {
      const p = f.properties;
      const xy = f.geometry.coordinates as number[];
      L.circleMarker([xy[1], xy[0]], { radius: 3, color: '#000', weight: 1, fillColor: '#111', fillOpacity: 1, pmIgnore: true })
        .bindTooltip(
          `camera position reported by source (not a feature location)<br>pano <code>${escapeHtml(p.pano_id ?? '—')}</code> · ${escapeHtml(p.side)} · ${escapeHtml(humanize(p.verification_status))}`,
          { direction: 'top' },
        )
        .addTo(g.cameras);
    }
    if (fitted.current !== c.job_id) {
      const b = boundsOfGeometries((c.samples?.features ?? []).map((f) => f.geometry));
      if (b) fitTo(map, b);
      fitted.current = c.job_id;
    }
  }, [map, cov.data]);

  const c = cov.data;
  return (
    <div className="coverage-layout">
      <div className="map-col">
        <div className="toolbar">
          <label className="inline-label">
            Job
            <JobSelect jobs={jobs.data} value={jobId} onChange={setJobId} testid="coverage-job" />
          </label>
          {job && (
            <span className="muted">
              {job.target} · {job.method} · status <strong>{job.status}</strong>
              {live ? ' · refreshing every 5 s' : ''}
            </span>
          )}
        </div>
        <div className="map-wrap">
          <div ref={mapEl} className="map" data-testid="coverage-map" />
          {!hasBasemap(status?.basemap) && <div className="map-nobase">No basemap configured — geometry only</div>}
        </div>
        <div className="legend-strip" aria-label="Map legend">
          {Object.entries(COVERAGE_COLORS).map(([k, col]) => (
            <div className="legend-item" key={k}>
              <span className={`lg-circle ${k === 'excluded' ? 'lg-hollow' : ''}`} style={k === 'excluded' ? { borderColor: col } : { background: col }} /> {humanize(k)}
            </div>
          ))}
          <div className="legend-item">
            <span className="lg-cam" /> camera position reported by source (not a feature location)
          </div>
          <div className="legend-item">
            <span className="lg-seg" /> plan segment
          </div>
        </div>
      </div>
      <div className="side side-right side-wide">
        {jobs.error && <ErrorList errors={jobs.error} />}
        {jobs.data && jobs.data.length === 0 && <Empty>No jobs yet — create one in the Run tab.</Empty>}
        {cov.error && <ErrorList errors={cov.error} title="Could not load coverage" />}
        {jobId && !c && !cov.error && <Loading what="coverage" />}
        {c && (
          <>
            <Section title="Completeness vs coverage" testid="coverage-stats">
              <Progress
                testid="coverage-completeness"
                label="Collection completeness — tasks finished (any outcome)"
                value={c.collection.completeness}
                sub={`${c.collection.tasks_terminal} / ${c.collection.tasks_total} tasks terminal`}
              />
              <Progress
                testid="coverage-verified"
                tone="ok"
                label="Verified coverage — planned views with a verified capture"
                value={c.verified_coverage}
                sub={`${c.views.verified ?? 0} / ${c.views.planned_views ?? 0} planned views`}
              />
              <Progress tone="teal" label="Covered incl. reuse and pending review" value={c.covered_including_reuse_and_review} />
              <CountChips counts={c.collection.task_states} order={TASK_STATES} />
            </Section>
            <Section title="Planned views by status">
              <table className="table table-compact">
                <tbody>
                  <tr>
                    <th>Planned views</th>
                    <td className="right">
                      <strong>{c.views.planned_views ?? 0}</strong>
                    </td>
                  </tr>
                  {VIEW_KEYS.map((k) => (
                    <tr key={k}>
                      <th>
                        <span className="lg-circle" style={{ background: COVERAGE_COLORS[k] }} /> {humanize(k)}
                      </th>
                      <td className="right">{c.views[k] ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
            <Section title="Association">
              {Object.keys(c.association ?? {}).length === 0 ? (
                <Empty>No associations yet.</Empty>
              ) : (
                <table className="table table-compact">
                  <tbody>
                    {Object.entries(c.association).map(([k, v]) => (
                      <tr key={k}>
                        <th>{humanize(k)}</th>
                        <td className="right">{v}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <KV
                rows={[
                  ['Distinct panoramas', fmtNum(c.distinct_panoramas)],
                  ['Panoramas shared by >1 sample', fmtNum(c.panoramas_shared_by_multiple_samples)],
                  ['Samples resolved by reuse', fmtNum(c.samples_resolved_by_reuse)],
                ]}
              />
            </Section>
            <Section title={`Gaps (${c.gaps.length})`}>
              <p className="hint">
                Measured from actual camera positions projected onto each segment side; threshold <strong>{fmtNum(c.gap_threshold_m, 1)} m</strong> (half at segment ends).
              </p>
              {c.gaps.length === 0 ? (
                <Empty>No gaps above the threshold.</Empty>
              ) : (
                <div className="table-scroll">
                  <table className="table table-compact" data-testid="gaps-table">
                    <thead>
                      <tr>
                        <th>Segment</th>
                        <th>Side</th>
                        <th className="right">From (m)</th>
                        <th className="right">To (m)</th>
                        <th className="right">Length (m)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {c.gaps.map((g, i) => (
                        <tr key={i}>
                          <td>
                            <Mono>{g.segment_key}</Mono>
                          </td>
                          <td>{g.side}</td>
                          <td className="right">{fmtNum(g.from_m, 1)}</td>
                          <td className="right">{fmtNum(g.to_m, 1)}</td>
                          <td className="right">{fmtNum(g.length_m, 1)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Section>
            <Section title="By segment side">
              <div className="table-scroll">
                <table className="table table-compact">
                  <thead>
                    <tr>
                      <th>Segment</th>
                      <th>Side</th>
                      {VIEW_KEYS.map((k) => (
                        <th key={k} className="right" title={humanize(k)}>
                          {humanize(k)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {c.by_segment_side.map((r, i) => (
                      <tr key={i}>
                        <td>
                          <Mono>{r.segment_key}</Mono>
                        </td>
                        <td>{r.side}</td>
                        {VIEW_KEYS.map((k) => (
                          <td key={k} className={`right ${r[k] ? '' : 'muted'}`}>
                            {Number(r[k] ?? 0)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
            <Section title="Notes">
              <ul className="notes">
                {c.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </Section>
          </>
        )}
      </div>
    </div>
  );
}
