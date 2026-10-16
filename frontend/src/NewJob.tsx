import { useEffect, useMemo, useRef, useState } from 'react';
import { api, DEFAULT_OPTIONS, type FC, type Feature, type Geo, type Job, type Options, type Preview, type Status } from './api';
import type { Cone, DrawMode } from './MapView';
import type { MapState } from './App';
import { metres } from './words';

type Props = { status: Status; setMap: (s: MapState) => void; onCancel: () => void; onCreated: (id: string) => void };

function lineLength(f: Feature): number {
  const parts: number[][][] = f.geometry.type === 'MultiLineString' ? f.geometry.coordinates : [f.geometry.coordinates];
  let m = 0;
  for (const c of parts)
    for (let i = 1; i < c.length; i++) {
      const [lng1, lat1] = c[i - 1], [lng2, lat2] = c[i];
      const k = Math.PI / 180;
      const a = Math.sin(((lat2 - lat1) * k) / 2) ** 2 + Math.cos(lat1 * k) * Math.cos(lat2 * k) * Math.sin(((lng2 - lng1) * k) / 2) ** 2;
      m += 2 * 6371008 * Math.asin(Math.sqrt(a));
    }
  return m;
}

export default function NewJob({ setMap, onCancel, onCreated }: Props) {
  const [source, setSource] = useState<'area' | 'street'>('area');
  const [drawing, setDrawing] = useState<DrawMode>('area');
  const [area, setArea] = useState<Geo | null>(null);
  const [streets, setStreets] = useState<FC | null>(null);
  const [finding, setFinding] = useState(false);
  const [opts, setOpts] = useState<Options>(DEFAULT_OPTIONS);
  const [more, setMore] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [fitKey, setFitKey] = useState('');
  const seq = useRef(0);

  const included: FC | null = useMemo(
    () => (streets ? { type: 'FeatureCollection', features: streets.features.filter((f) => f.properties.include !== false) } : null),
    [streets],
  );

  function onDrawn(g: Geo, mode: 'area' | 'street') {
    setError('');
    if (mode === 'area') {
      setArea(g);
      setDrawing(null);
      setStreets(null);
      setFinding(true);
      api
        .post<FC>('/api/streets', { area: g })
        .then((fc) => {
          setStreets({ type: 'FeatureCollection', features: fc.features.map((f) => ({ ...f, properties: { ...f.properties, include: true } })) });
          if (fc.features.length === 0) setError('No streets found in that area. Try a larger area, or draw the streets yourself.');
        })
        .catch((e) => setError(e.message))
        .finally(() => setFinding(false));
      setFitKey(`area-${Date.now()}`);
    } else {
      setStreets((s) => {
        const n = (s?.features.length ?? 0) + 1;
        const f: Feature = { type: 'Feature', geometry: g, properties: { name: `Street ${n}`, include: true } };
        return { type: 'FeatureCollection', features: [...(s?.features ?? []), f] };
      });
    }
  }

  // live preview of every planned photo
  useEffect(() => {
    if (!included || included.features.length === 0) {
      setPreview(null);
      return;
    }
    const my = ++seq.current;
    const t = setTimeout(() => {
      api
        .post<Preview>('/api/preview', { streets: included, options: opts })
        .then((p) => my === seq.current && setPreview(p))
        .catch((e) => my === seq.current && setError(e.message));
    }, 300);
    return () => clearTimeout(t);
  }, [included, opts]);

  const cones: Cone[] = useMemo(
    () => (preview?.shots ?? []).map((s) => ({ id: s.code, lat: s.lat, lng: s.lng, heading: s.heading, fov: s.fov, tone: (s.side === 'left' ? 'left' : s.side === 'right' ? 'right' : 'none') as Cone['tone'], label: `${s.code}: ${s.look}` })),
    [preview],
  );

  useEffect(() => {
    setMap({ area, streets, cones, drawMode: drawing, onDrawn, fitKey });
  }, [area, streets, cones, drawing, fitKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // street names for the list (an OSM street can come in several pieces)
  const names = useMemo(() => {
    const m = new Map<string, { length: number; include: boolean }>();
    for (const f of streets?.features ?? []) {
      const k = f.properties.name || 'Unnamed street';
      const cur = m.get(k) ?? { length: 0, include: false };
      m.set(k, { length: cur.length + lineLength(f), include: cur.include || f.properties.include !== false });
    }
    return [...m.entries()];
  }, [streets]);

  function toggleName(n: string, include: boolean) {
    setStreets((s) => s && { ...s, features: s.features.map((f) => ((f.properties.name || 'Unnamed street') === n ? { ...f, properties: { ...f.properties, include } } : f)) });
  }

  function chooseSource(s: 'area' | 'street') {
    setSource(s);
    setArea(null);
    setStreets(null);
    setPreview(null);
    setError('');
    setDrawing(s);
  }

  async function create() {
    if (!included) return;
    setCreating(true);
    setError('');
    try {
      const job = await api.post<Job>('/api/jobs', { name: name.trim() || defaultName, area, streets: included, options: opts });
      onCreated(job.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  }

  const chosen = names.filter(([, v]) => v.include).sort((a, b) => b[1].length - a[1].length).map(([n]) => n).filter((n) => n !== 'Unnamed street');
  const defaultName = chosen.length === 0 ? 'New job' : chosen.length <= 2 ? chosen.join(' and ') : `${chosen[0]}, ${chosen[1]} and ${chosen.length - 2} more`;
  const set = <K extends keyof Options>(k: K, v: Options[K]) => setOpts((o) => ({ ...o, [k]: v }));

  return (
    <div className="panel-body">
      <div className="home-head">
        <h1>New job</h1>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
      </div>

      <section className="step">
        <h2><span className="step-n">1</span>Choose streets</h2>
        <div className="seg" role="group" aria-label="How to choose streets">
          <button type="button" aria-pressed={source === 'area'} onClick={() => chooseSource('area')}>Block out an area</button>
          <button type="button" aria-pressed={source === 'street'} onClick={() => chooseSource('street')}>Draw streets</button>
        </div>

        {source === 'area' && !area && (
          <p className="hint">Click around the block on the map. Click the first point again to close the shape.</p>
        )}
        {source === 'area' && area && (
          <>
            {finding && <p className="hint">Finding streets in this area…</p>}
            {names.length > 0 && (
              <>
                <div className="list-head">
                  <p>{names.filter(([, v]) => v.include).length} of {names.length} streets, {metres(names.reduce((t, [, v]) => t + (v.include ? v.length : 0), 0))}</p>
                  <button type="button" className="btn btn-quiet" onClick={() => names.forEach(([n]) => toggleName(n, names.some(([, v]) => !v.include)))}>
                    {names.some(([, v]) => !v.include) ? 'Select all' : 'Clear all'}
                  </button>
                </div>
                <ul className="checklist checklist-scroll">
                  {names.map(([n, v]) => (
                    <li key={n}>
                      <label>
                        <input type="checkbox" checked={v.include} onChange={(e) => toggleName(n, e.target.checked)} />
                        <span>{n}</span>
                        <span className="muted">{metres(v.length)}</span>
                      </label>
                    </li>
                  ))}
                </ul>
                <p className="fineprint">Street data © OpenStreetMap contributors.</p>
              </>
            )}
            <button type="button" className="btn btn-quiet" onClick={() => chooseSource('area')}>Redraw the area</button>
          </>
        )}

        {source === 'street' && (
          <>
            <p className="hint">Click along a street, then click the last point again to finish. Draw as many as you need.</p>
            {names.length > 0 && (
              <ul className="checklist">
                {(streets?.features ?? []).map((f, i) => (
                  <li key={i} className="street-edit">
                    <input
                      aria-label={`Name of street ${i + 1}`}
                      value={f.properties.name}
                      onChange={(e) => setStreets((s) => s && { ...s, features: s.features.map((g, j) => (j === i ? { ...g, properties: { ...g.properties, name: e.target.value } } : g)) })}
                    />
                    <span className="muted">{metres(lineLength(f))}</span>
                    <button type="button" className="btn btn-quiet" onClick={() => setStreets((s) => s && { ...s, features: s.features.filter((_, j) => j !== i) })}>Remove</button>
                  </li>
                ))}
              </ul>
            )}
            {drawing !== 'street' && <button type="button" className="btn" onClick={() => setDrawing('street')}>Draw another street</button>}
          </>
        )}
      </section>

      <section className="step" aria-disabled={!preview}>
        <h2><span className="step-n">2</span>Plan the photos</h2>
        <label className="field">
          <span>Distance between photo points</span>
          <span className="input-unit">
            <input type="number" min={5} max={200} value={opts.spacing_m} onChange={(e) => set('spacing_m', Number(e.target.value) || 15)} data-testid="spacing" />
            <span>m</span>
          </span>
        </label>
        <div className="field">
          <span>Footpaths</span>
          <div className="seg" role="group" aria-label="Footpaths">
            {(['both', 'left', 'right'] as const).map((v) => (
              <button key={v} type="button" aria-pressed={opts.sides === v} onClick={() => set('sides', v)}>
                {v === 'both' ? 'Both sides' : v === 'left' ? 'Left of arrow' : 'Right of arrow'}
              </button>
            ))}
          </div>
        </div>
        <div className="field">
          <span>Views at each point</span>
          <div className="seg" role="group" aria-label="Views at each point">
            {[1, 2, 3].map((v) => (
              <button key={v} type="button" aria-pressed={opts.angles === v} onClick={() => set('angles', v)}>
                {v === 1 ? 'Straight across' : v === 2 ? '+ angled ahead' : '+ angled both ways'}
              </button>
            ))}
          </div>
        </div>
        <label className="check">
          <input type="checkbox" checked={opts.corners} onChange={(e) => set('corners', e.target.checked)} />
          <span>Also photograph into each intersection</span>
        </label>

        <button type="button" className="disclosure" aria-expanded={more} onClick={() => setMore(!more)}>Camera and prompt options</button>
        {more && (
          <div className="more">
            <label className="field"><span>Camera tilt</span><span className="input-unit"><input type="number" min={-40} max={20} value={opts.pitch} onChange={(e) => set('pitch', Number(e.target.value))} /><span>°</span></span></label>
            <label className="field"><span>Field of view</span><span className="input-unit"><input type="number" min={30} max={100} value={opts.fov} onChange={(e) => set('fov', Number(e.target.value) || 80)} /><span>°</span></span></label>
            <label className="field"><span>Photos per prompt</span><span className="input-unit"><input type="number" min={5} max={100} value={opts.batch_size} onChange={(e) => set('batch_size', Number(e.target.value) || 20)} /></span></label>
            <p className="fineprint">Negative tilt looks down toward the kerb. Smaller prompts are easier for Codex to finish in one go.</p>
          </div>
        )}

        {preview && (
          <p className="summary" data-testid="plan-summary">
            <strong>{preview.summary.shots} photos</strong> at {preview.summary.points} points along {metres(preview.summary.length_m)}, split into {preview.summary.batches} prompt{preview.summary.batches === 1 ? '' : 's'}.
          </p>
        )}
        {preview?.warnings.map((w) => <p key={w} className="note">{w}</p>)}
      </section>

      <section className="step" aria-disabled={!preview}>
        <h2><span className="step-n">3</span>Name the job</h2>
        <label className="field">
          <span className="sr-only">Job name</span>
          <input value={name} placeholder={defaultName} onChange={(e) => setName(e.target.value)} data-testid="job-name" />
        </label>
        <button type="button" className="btn btn-primary btn-wide" disabled={!preview || creating} onClick={create} data-testid="create-job">
          {creating ? 'Creating…' : 'Create job'}
        </button>
      </section>

      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
