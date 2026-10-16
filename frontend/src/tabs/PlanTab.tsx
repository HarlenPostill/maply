import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { TabProps } from '../App';
import { api } from '../lib/api';
import { useAction, useResource } from '../lib/hooks';
import type { Feature, Geometry, ParsedStreetView, Plan, PlanDetail, RouteRow, SampleProps } from '../lib/types';
import { fmtCoord, fmtDeg, fmtNum, fmtTime, humanize, shortId } from '../lib/format';
import { lineMidpoint, parseLngLatPairs } from '../lib/geo';
import {
  L,
  SIDE_COLORS,
  arrowIcon,
  boundsOfGeometries,
  diamondIcon,
  divIcon,
  escapeHtml,
  fitTo,
  geometryLayer,
  hasBasemap,
  headingRay,
  scopeLayer,
  useLeafletMap,
} from '../lib/map';
import { Chip, Empty, ErrorList, Field, KV, Loading, Mono, Notice, Section } from '../components/common';
import { PlanParamsForm, formToParams, paramsToForm, type ParamForm } from '../components/PlanParamsForm';

const ROUTE_COLOR = '#6d28d9';
const SEED_KIND = 'streetview_seed';

interface RouteBody {
  name: string;
  kind: string;
  geojson: Geometry;
  properties?: Record<string, unknown>;
}

function routeStyle(kind: string, included: boolean): L.PolylineOptions {
  return {
    color: ROUTE_COLOR,
    weight: included ? 4 : 3,
    opacity: included ? 0.9 : 0.55,
    dashArray: included ? undefined : '6 6',
    fill: kind === 'polygon' || undefined,
    fillOpacity: 0.06,
  };
}

function sampleIcon(p: SampleProps, selected: boolean): L.DivIcon {
  const cls = ['smp', `smp-${p.kind}`];
  if (p.excluded) cls.push('smp-excluded');
  if (p.edited) cls.push('smp-edited');
  if (selected) cls.push('smp-selected');
  return divIcon('smp-icon', `<div class="${cls.join(' ')}"></div>`, 14);
}

export function PlanTab({ project, status, active, planId, setPlanId }: TabProps) {
  const pid = project.id;
  const routes = useResource(`routes:${pid}`, () => api.get<RouteRow[]>(`/api/projects/${pid}/routes`));
  const plans = useResource(`plans:${pid}`, () => api.get<Plan[]>(`/api/projects/${pid}/plans`));
  const plan = useResource(planId ? `plan:${planId}` : null, () => api.get<PlanDetail>(`/api/plans/${planId}`));

  // Auto-select the newest plan so the preview shows something useful.
  useEffect(() => {
    if (!planId && plans.data && plans.data.length > 0) setPlanId(plans.data[0].id);
  }, [planId, plans.data, setPlanId]);

  // ------------------------------------------------------------ route selection
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const known = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!routes.data) return;
    const add: string[] = [];
    for (const r of routes.data) {
      if (!known.current.has(r.id)) {
        known.current.add(r.id);
        if (r.kind !== SEED_KIND) add.push(r.id);
      }
    }
    const ids = new Set(routes.data.map((r) => r.id));
    setSelected((prev) => {
      const n = new Set([...prev].filter((id) => ids.has(id)));
      add.forEach((id) => n.add(id));
      return n;
    });
  }, [routes.data]);

  const [routeName, setRouteName] = useState('');
  const [routeMsgs, setRouteMsgs] = useState<{ tone: 'ok' | 'warn'; text: string }[]>([]);
  const [routeWarnings, setRouteWarnings] = useState<Record<string, string[]>>({});
  const routeAct = useAction();

  const noteRouteResult = useCallback((r: RouteRow | RouteRow[], verb: string) => {
    const list = Array.isArray(r) ? r : [r];
    const w: Record<string, string[]> = {};
    const msgs: { tone: 'ok' | 'warn'; text: string }[] = [{ tone: 'ok', text: `${verb} ${list.length} route${list.length === 1 ? '' : 's'}` }];
    for (const x of list) {
      if (x.warnings && x.warnings.length) {
        w[x.id] = x.warnings;
        x.warnings.forEach((t) => msgs.push({ tone: 'warn', text: `${x.name || shortId(x.id)}: ${t}` }));
      }
    }
    setRouteWarnings((prev) => {
      const n = { ...prev };
      list.forEach((x) => {
        if (w[x.id]) n[x.id] = w[x.id];
        else delete n[x.id];
      });
      return n;
    });
    setRouteMsgs(msgs);
  }, []);

  const createRoute = useCallback(
    async (body: RouteBody) => {
      const r = await routeAct.run(() => api.post<RouteRow>(`/api/projects/${pid}/routes`, body));
      if (r) {
        noteRouteResult(r, 'Saved');
        await routes.reload();
      }
      return r;
    },
    [pid, routeAct, routes, noteRouteResult],
  );

  const deleteRoute = useCallback(
    async (rid: string) => {
      const ok = await routeAct.run(() => api.del(`/api/routes/${rid}`));
      setRouteWarnings((prev) => {
        const n = { ...prev };
        delete n[rid];
        return n;
      });
      if (ok !== undefined) setRouteMsgs([{ tone: 'ok', text: 'Route deleted (existing plans keep their own copy of the geometry)' }]);
      await routes.reload();
      return ok !== undefined;
    },
    [routeAct, routes],
  );

  // ------------------------------------------------------------ map
  const mapEl = useRef<HTMLDivElement>(null);
  const map = useLeafletMap(mapEl, status?.basemap);
  const groups = useRef<{ scope: L.LayerGroup; routes: L.FeatureGroup; plan: L.LayerGroup } | null>(null);
  const routeLayers = useRef<Map<string, { layer: L.Layer; hash: string }>>(new Map());
  const layerRoute = useRef<WeakMap<L.Layer, string>>(new WeakMap());
  const putTimers = useRef<Map<string, number>>(new Map());
  const sampleMarkers = useRef<Map<string, { marker: L.Marker; props: SampleProps }>>(new Map());
  const [selectedSample, setSelectedSample] = useState<string | null>(null);
  const selectedSampleRef = useRef<string | null>(null);
  const [showRoutes, setShowRoutes] = useState(true);
  const [showPlan, setShowPlan] = useState(true);
  const sampleAct = useAction();

  // Latest-value refs for Leaflet event handlers (avoid stale closures).
  const handlers = useRef({
    onCreate: (_shape: string, _layer: L.Layer) => {},
    onRemove: (_layer: L.Layer) => {},
    onEdit: (_rid: string) => {},
    onSampleMoved: (_sid: string, _m: L.Marker, _orig: L.LatLng) => {},
  });

  handlers.current.onCreate = (shape, layer) => {
    const geom = (layer as L.Polyline).toGeoJSON().geometry as Geometry;
    layer.remove();
    const kind = shape === 'Polygon' ? 'polygon' : 'route';
    const n = routeName.trim() || `${kind === 'polygon' ? 'Polygon' : 'Route'} ${fmtTime(new Date().toISOString()).slice(11)}`;
    void createRoute({ name: n, kind, geojson: geom, properties: {} });
  };
  handlers.current.onRemove = (layer) => {
    const rid = layerRoute.current.get(layer);
    if (!rid) return;
    routeLayers.current.delete(rid);
    groups.current?.routes.removeLayer(layer);
    void deleteRoute(rid);
  };
  handlers.current.onEdit = (rid) => {
    const prev = putTimers.current.get(rid);
    if (prev) window.clearTimeout(prev);
    putTimers.current.set(
      rid,
      window.setTimeout(async () => {
        putTimers.current.delete(rid);
        const ent = routeLayers.current.get(rid);
        const r = routes.data?.find((x) => x.id === rid);
        if (!ent || !r) return;
        const geom = (ent.layer as L.Polyline).toGeoJSON().geometry as Geometry;
        const res = await routeAct.run(() =>
          api.put<RouteRow>(`/api/routes/${rid}`, { name: r.name, kind: r.kind, geojson: geom, properties: r.properties ?? {} }),
        );
        if (res) {
          ent.hash = JSON.stringify(res.geometry);
          noteRouteResult(res, 'Updated');
          if (routes.data) routes.setData(routes.data.map((x) => (x.id === rid ? { ...res } : x)));
        } else {
          // Restore server geometry on failure.
          routeLayers.current.delete(rid);
          groups.current?.routes.removeLayer(ent.layer);
          await routes.reload();
        }
      }, 600),
    );
  };
  handlers.current.onSampleMoved = (sid, marker, orig) => {
    const ll = marker.getLatLng();
    void (async () => {
      const res = await sampleAct.run(() => api.patch<PlanDetail>(`/api/samples/${sid}`, { lng: ll.lng, lat: ll.lat }));
      if (res) {
        plan.setData(res);
        setSelectedSample(sid);
      } else {
        marker.setLatLng(orig);
      }
    })();
  };

  // Map layer groups + Geoman toolbar.
  useEffect(() => {
    if (!map) return;
    const g = {
      scope: L.layerGroup([], { pmIgnore: true }).addTo(map),
      routes: L.featureGroup().addTo(map),
      plan: L.layerGroup([], { pmIgnore: true }).addTo(map),
    };
    groups.current = g;
    map.pm.addControls({
      position: 'topleft',
      drawMarker: false,
      drawCircleMarker: false,
      drawRectangle: false,
      drawCircle: false,
      drawText: false,
      cutPolygon: false,
      rotateMode: false,
      drawPolyline: true,
      drawPolygon: true,
      editMode: true,
      dragMode: true,
      removalMode: true,
    });
    map.pm.setGlobalOptions({ snappable: true, snapDistance: 12, pathOptions: { color: ROUTE_COLOR, weight: 4 } });
    const onCreate = (e: { shape: string; layer: L.Layer }) => handlers.current.onCreate(e.shape, e.layer);
    const onRemove = (e: { layer: L.Layer }) => handlers.current.onRemove(e.layer);
    map.on('pm:create', onCreate);
    map.on('pm:remove', onRemove);
    const layersRef = routeLayers.current;
    const timers = putTimers.current;
    return () => {
      map.off('pm:create', onCreate);
      map.off('pm:remove', onRemove);
      try {
        map.pm.removeControls();
      } catch {
        /* map already removed */
      }
      layersRef.clear();
      timers.forEach((t) => window.clearTimeout(t));
      timers.clear();
      groups.current = null;
    };
  }, [map]);

  // Scope outline.
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    g.scope.clearLayers();
    const s = scopeLayer(project.scope);
    if (s) g.scope.addLayer(s);
  }, [map, project.scope]);

  // Saved routes (editable through Geoman). Layers are synced by id so that
  // edits in progress are not destroyed by re-renders.
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    const list = routes.data ?? [];
    const seen = new Set<string>();
    for (const r of list) {
      seen.add(r.id);
      const included = selected.has(r.id);
      const isSeed = r.kind === SEED_KIND || r.geometry?.type === 'Point';
      const hash = JSON.stringify(r.geometry) + (isSeed ? `|${included}` : '');
      const ent = routeLayers.current.get(r.id);
      if (ent && ent.hash === hash) {
        if (!isSeed && ent.layer instanceof L.Path) ent.layer.setStyle(routeStyle(r.kind, included));
        continue;
      }
      if (ent) g.routes.removeLayer(ent.layer);
      let layer: L.Layer | null = null;
      const label = `<strong>${escapeHtml(r.name || '(unnamed)')}</strong><br>${escapeHtml(humanize(r.kind))}${included ? '' : ' · not included in plan'}`;
      if (isSeed) {
        const c = r.geometry.coordinates as number[];
        const lat = c[1];
        const lng = c[0];
        const heading = Number(r.properties?.heading);
        const grp = L.layerGroup([], { pmIgnore: true });
        if (Number.isFinite(heading)) headingRay(lat, lng, heading, 15, { color: '#a67c00', weight: 3 }).addTo(grp);
        L.circleMarker([lat, lng], {
          radius: 7,
          color: '#7a5a00',
          weight: 2,
          fillColor: '#f2c230',
          fillOpacity: included ? 1 : 0.45,
          dashArray: included ? undefined : '2 2',
          pmIgnore: true,
        })
          .bindTooltip(`${label}<br>Street View seed${Number.isFinite(heading) ? ` · heading ${heading.toFixed(1)}°` : ''}`, { sticky: true })
          .addTo(grp);
        layer = grp;
      } else {
        const pl = geometryLayer(r.geometry, routeStyle(r.kind, included));
        if (pl) {
          pl.bindTooltip(label, { sticky: true });
          pl.on('pm:edit', () => handlers.current.onEdit(r.id));
          layer = pl;
        }
      }
      if (!layer) continue;
      layerRoute.current.set(layer, r.id);
      g.routes.addLayer(layer);
      routeLayers.current.set(r.id, { layer, hash });
    }
    for (const [id, ent] of [...routeLayers.current.entries()]) {
      if (!seen.has(id)) {
        g.routes.removeLayer(ent.layer);
        routeLayers.current.delete(id);
      }
    }
  }, [map, routes.data, selected]);

  // Layer visibility toggles.
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    if (showRoutes) g.routes.addTo(map);
    else g.routes.remove();
  }, [map, showRoutes]);
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    if (showPlan) g.plan.addTo(map);
    else g.plan.remove();
  }, [map, showPlan]);

  // Plan preview (segments, footpath offsets, samples, view rays, nodes).
  useEffect(() => {
    const g = groups.current;
    if (!map || !g) return;
    g.plan.clearLayers();
    sampleMarkers.current.clear();
    const pd = plan.data;
    if (!pd || !pd.geojson) return;
    const gj = pd.geojson;
    const draft = pd.status === 'draft';

    for (const f of gj.footpaths?.features ?? []) {
      const l = geometryLayer(f.geometry, {
        color: SIDE_COLORS[f.properties.side] ?? SIDE_COLORS.none,
        weight: 2,
        opacity: 0.9,
        dashArray: '4 6',
        interactive: false,
        pmIgnore: true,
        snapIgnore: true,
      });
      l?.addTo(g.plan);
    }
    for (const f of gj.segments?.features ?? []) {
      const p = f.properties;
      const l = geometryLayer(f.geometry, { color: '#334155', weight: 9, opacity: 0.32, lineCap: 'butt', pmIgnore: true, snapIgnore: true });
      if (!l) continue;
      l.bindTooltip(
        `<strong>${escapeHtml(p.name || p.segment_key)}</strong><br><code>${escapeHtml(p.segment_key)}</code><br>${fmtNum(p.length_m, 1)} m · bearing ${fmtDeg(p.bearing_deg)} · sides ${escapeHtml((p.sides ?? []).join('/'))}`,
        { sticky: true },
      );
      l.addTo(g.plan);
      const coords = (f.geometry.type === 'LineString' ? f.geometry.coordinates : []) as [number, number][];
      const mid = lineMidpoint(coords);
      if (mid && Number.isFinite(p.bearing_deg)) {
        L.marker([mid[1], mid[0]], { icon: arrowIcon(p.bearing_deg), interactive: false, keyboard: false, pmIgnore: true }).addTo(g.plan);
      }
    }
    for (const f of gj.samples?.features ?? []) {
      const c = f.geometry.coordinates as number[];
      for (const v of f.properties.views ?? []) {
        headingRay(c[1], c[0], v.heading_deg, 8, {
          color: SIDE_COLORS[v.side] ?? SIDE_COLORS.none,
          weight: 2.5,
          opacity: f.properties.excluded ? 0.3 : 0.95,
          snapIgnore: true,
        }).addTo(g.plan);
      }
    }
    for (const f of gj.nodes?.features ?? []) {
      const c = f.geometry.coordinates as number[];
      const p = f.properties;
      L.marker([c[1], c[0]], { icon: diamondIcon(`node-${p.kind}`), keyboard: false, pmIgnore: true })
        .bindTooltip(`<strong>${escapeHtml(humanize(p.kind))}</strong> — heuristic candidate<br><code>${escapeHtml(p.node_key)}</code>`, { direction: 'top' })
        .addTo(g.plan);
    }
    for (const f of gj.samples?.features ?? []) {
      const c = f.geometry.coordinates as number[];
      const p = f.properties;
      const m = L.marker([c[1], c[0]], {
        icon: sampleIcon(p, selectedSampleRef.current === p.id),
        draggable: draft,
        autoPan: true,
        keyboard: false,
        pmIgnore: true,
        zIndexOffset: 500,
      });
      m.bindTooltip(
        `#${p.ordinal} · ${escapeHtml(humanize(p.kind))}${p.excluded ? ' · excluded' : ''}${p.edited ? ' · edited' : ''}${draft ? '<br><span class="muted">drag to adjust</span>' : ''}`,
        { direction: 'top', offset: [0, -8] },
      );
      m.on('click', () => setSelectedSample(p.id));
      if (draft) {
        let orig = m.getLatLng();
        m.on('dragstart', () => {
          orig = m.getLatLng();
        });
        m.on('dragend', () => handlers.current.onSampleMoved(p.id, m, orig));
      }
      m.addTo(g.plan);
      sampleMarkers.current.set(p.id, { marker: m, props: p });
    }
  }, [map, plan.data]);

  // Highlight the selected sample without rebuilding the layer.
  useEffect(() => {
    const prev = selectedSampleRef.current;
    selectedSampleRef.current = selectedSample;
    for (const id of [prev, selectedSample]) {
      if (!id) continue;
      const e = sampleMarkers.current.get(id);
      if (e) e.marker.setIcon(sampleIcon(e.props, id === selectedSample));
    }
  }, [selectedSample]);

  useEffect(() => {
    setSelectedSample(null);
  }, [planId]);

  // Fit the view once per plan (or to the routes if there is no plan).
  const fittedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!map) return;
    if (plan.data && fittedFor.current !== `plan:${plan.data.id}`) {
      const b = boundsOfGeometries((plan.data.geojson?.segments?.features ?? []).map((f) => f.geometry));
      if (b) fitTo(map, b);
      fittedFor.current = `plan:${plan.data.id}`;
    } else if (!planId && routes.data && routes.data.length && fittedFor.current === null) {
      fitTo(map, boundsOfGeometries(routes.data.map((r) => r.geometry)));
      fittedFor.current = 'routes';
    }
  }, [map, plan.data, routes.data, planId]);

  // When the tab becomes visible again Leaflet needs a size refresh.
  useEffect(() => {
    if (active && map) window.setTimeout(() => map.invalidateSize(), 0);
  }, [active, map]);

  // ------------------------------------------------------------ plan generation
  const [paramForm, setParamForm] = useState<ParamForm>(() => paramsToForm(null));
  const [paramErrors, setParamErrors] = useState<Record<string, string>>({});
  const [planName, setPlanName] = useState('');
  const genAct = useAction();
  const planAct = useAction();

  const generate = async () => {
    const { params, errors } = formToParams(paramForm);
    setParamErrors(errors);
    if (!params) {
      genAct.setError(['Fix the highlighted plan parameters']);
      return;
    }
    const ids = (routes.data ?? []).filter((r) => selected.has(r.id)).map((r) => r.id);
    if (!ids.length) {
      genAct.setError(['Select at least one route to include in the plan']);
      return;
    }
    const res = await genAct.run(() => api.post<PlanDetail>(`/api/projects/${pid}/plans`, { name: planName.trim(), route_ids: ids, params, seed_urls: [] }));
    if (res) {
      setPlanId(res.id);
      plan.setData(res);
      await plans.reload();
    }
  };

  const approve = async () => {
    if (!plan.data) return;
    const res = await planAct.run(() => api.post<Plan>(`/api/plans/${plan.data!.id}/approve`));
    if (res) {
      await Promise.all([plan.reload(), plans.reload()]);
    }
  };

  const deletePlan = async () => {
    if (!plan.data) return;
    if (!window.confirm(`Delete plan "${plan.data.name}"? Plans that already have jobs cannot be deleted.`)) return;
    const res = await planAct.run(() => api.del(`/api/plans/${plan.data!.id}`));
    if (res !== undefined) {
      setPlanId(null);
      await plans.reload();
    }
  };

  const selSample: Feature<SampleProps> | undefined = useMemo(
    () => plan.data?.geojson?.samples?.features?.find((f) => f.properties.id === selectedSample),
    [plan.data, selectedSample],
  );

  const patchSample = async (body: Record<string, unknown>) => {
    if (!selSample) return;
    const res = await sampleAct.run(() => api.patch<PlanDetail>(`/api/samples/${selSample.properties.id}`, body));
    if (res) plan.setData(res);
  };

  const panTo = (lat: number, lng: number, zoom = 18) => map?.setView([lat, lng], zoom);

  const pd = plan.data;
  const summary = pd?.summary ?? {};
  const isDraft = pd?.status === 'draft';
  const routeList = routes.data ?? [];

  return (
    <div className="plan-layout">
      {/* ------------------------------------------------ left: routes + params */}
      <div className="side side-left">
        <Section title="Routes" actions={<span className="muted">{routeList.length} saved</span>}>
          <p className="hint">
            Draw with the map toolbar: <strong>line</strong> = route, <strong>polygon</strong> = area. Edit, drag and remove modes save immediately.
          </p>
          <Field label="Name for the next route" htmlFor="route-name">
            <input id="route-name" value={routeName} onChange={(e) => setRouteName(e.target.value)} placeholder="e.g. George St (Bathurst → Park)" />
          </Field>
          <RouteByCoords onAdd={(coords) => createRoute({ name: routeName.trim() || 'Route (coordinates)', kind: 'route', geojson: { type: 'LineString', coordinates: coords } })} busy={routeAct.busy} />
          <ImportGeoJSON
            pid={pid}
            onImported={async (rows) => {
              noteRouteResult(rows, 'Imported');
              await routes.reload();
            }}
          />
          <StreetViewSeed
            busy={routeAct.busy}
            onAdd={async (p, url) => {
              const r = await createRoute({
                name: routeName.trim() || `Street View seed${p.pano_id ? ` ${p.pano_id.slice(0, 10)}` : ''}`,
                kind: SEED_KIND,
                geojson: { type: 'Point', coordinates: [p.lng!, p.lat!] },
                properties: { heading: p.heading, pitch: p.pitch, fov: p.fov, pano_id: p.pano_id, source_url: url },
              });
              if (r) panTo(p.lat!, p.lng!);
            }}
          />
          <ErrorList errors={routeAct.error} />
          {routeMsgs.length > 0 && (
            <div className="msgs">
              {routeMsgs.map((m, i) => (
                <Notice key={i} tone={m.tone}>
                  {m.text}
                </Notice>
              ))}
            </div>
          )}
          {routes.error && <ErrorList errors={routes.error} />}
          {!routes.data && !routes.error && <Loading what="routes" />}
          {routes.data && routeList.length === 0 && <Empty>No routes yet. Draw one on the map or enter coordinates.</Empty>}
          {routeList.length > 0 && (
            <table className="table table-compact routes-table" data-testid="routes-list">
              <thead>
                <tr>
                  <th title="Include in the next plan">Use</th>
                  <th>Name</th>
                  <th>Kind</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {routeList.map((r) => (
                  <tr key={r.id} data-testid="route-row">
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Include ${r.name || 'route'} in plan`}
                        checked={selected.has(r.id)}
                        onChange={(e) =>
                          setSelected((prev) => {
                            const n = new Set(prev);
                            if (e.target.checked) n.add(r.id);
                            else n.delete(r.id);
                            return n;
                          })
                        }
                      />
                    </td>
                    <td>
                      <button type="button" className="btn-link" onClick={() => map && fitTo(map, boundsOfGeometries([r.geometry]))} title="Zoom to route">
                        {r.name || <span className="muted">(unnamed)</span>}
                      </button>
                      {routeWarnings[r.id]?.map((w, i) => (
                        <div key={i} className="warn-text">
                          ⚠ {w}
                        </div>
                      ))}
                    </td>
                    <td>
                      <Chip tone={r.kind === SEED_KIND ? 'warn' : 'neutral'}>{humanize(r.kind)}</Chip>
                    </td>
                    <td className="right">
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => void deleteRoute(r.id)} aria-label={`Delete ${r.name || 'route'}`}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {routeList.some((r) => r.kind === SEED_KIND) && <p className="hint">Street View seeds are not included by default; tick them to add seed samples.</p>}
        </Section>

        <Section title="Plan parameters" actions={pd ? <button type="button" className="btn btn-sm btn-ghost" onClick={() => setParamForm(paramsToForm(pd.params))} title="Copy parameters from the selected plan">Load from selected plan</button> : undefined}>
          <PlanParamsForm form={paramForm} onChange={setParamForm} errors={paramErrors} />
          <div className="row gap wrap">
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setParamForm(paramsToForm(null)); setParamErrors({}); }}>
              Reset to defaults
            </button>
          </div>
          <Field label="Plan name (optional)" htmlFor="plan-name">
            <input id="plan-name" value={planName} onChange={(e) => setPlanName(e.target.value)} placeholder="defaults to a timestamp" />
          </Field>
          <div className="row gap">
            <button type="button" className="btn btn-primary" data-testid="generate-plan" onClick={() => void generate()} disabled={genAct.busy}>
              {genAct.busy ? 'Generating…' : 'Generate capture plan'}
            </button>
            <span className="muted">{[...selected].filter((id) => routeList.some((r) => r.id === id)).length} route(s) selected</span>
          </div>
          <ErrorList errors={genAct.error} />
        </Section>
      </div>

      {/* ------------------------------------------------ centre: map */}
      <div className="map-col">
        <div className="map-wrap">
          <div ref={mapEl} className="map" data-testid="plan-map" />
          {!hasBasemap(status?.basemap) && <div className="map-nobase">No basemap configured — geometry only</div>}
        </div>
        <div className="legend-strip" aria-label="Map legend">
          <div className="legend-toggles">
            <label>
              <input type="checkbox" checked={showRoutes} onChange={(e) => setShowRoutes(e.target.checked)} /> routes
            </label>
            <label>
              <input type="checkbox" checked={showPlan} onChange={(e) => setShowPlan(e.target.checked)} /> plan preview
            </label>
          </div>
          <div className="legend-item">
            <span className="lg-line" style={{ borderColor: ROUTE_COLOR }} /> saved route (dashed = not selected)
          </div>
          <div className="legend-item">
            <span className="lg-line lg-dashed" style={{ borderColor: '#3d4a5c' }} /> approved scope (approximate)
          </div>
          <div className="legend-item">
            <span className="lg-seg" /> <span className="map-arrow lg-arrow" /> segment · arrow = stored direction
          </div>
          <div className="legend-item">
            <span className="lg-line lg-dashed" style={{ borderColor: SIDE_COLORS.left }} />
            <span className="lg-line lg-dashed" style={{ borderColor: SIDE_COLORS.right }} /> footpath L / R — approximate offset, not surveyed
          </div>
          <div className="legend-item">
            <span className="smp smp-street lg-dot" /> street <span className="smp smp-crossing_approach lg-dot" /> crossing approach <span className="smp smp-seed lg-dot" /> seed
          </div>
          <div className="legend-item">
            <span className="smp smp-street smp-excluded lg-dot" /> excluded <span className="smp smp-street smp-edited lg-dot" /> edited
          </div>
          <div className="legend-item">
            view rays: <span className="lg-ray" style={{ background: SIDE_COLORS.left }} /> left <span className="lg-ray" style={{ background: SIDE_COLORS.right }} /> right{' '}
            <span className="lg-ray" style={{ background: SIDE_COLORS.none }} /> none
          </div>
          <div className="legend-item">
            <span className="map-diamond lg-diamond" /> node — heuristic candidate
          </div>
        </div>
        <p className="map-caption">
          Proposed orientations come from the local route bearing; they are not proof the footpath is visible. Left/right are relative to each segment&apos;s stored direction
          (arrow).
          {isDraft && <strong> Draft plan: drag samples to adjust; click a sample for details.</strong>}
        </p>
        <ErrorList errors={sampleAct.error} title="Sample edit refused" />
      </div>

      {/* ------------------------------------------------ right: plans + summary + sample */}
      <div className="side side-right">
        <Section title="Capture plans" actions={<span className="muted">{plans.data?.length ?? 0}</span>}>
          {plans.error && <ErrorList errors={plans.error} />}
          {!plans.data && !plans.error && <Loading what="plans" />}
          {plans.data && plans.data.length === 0 && <Empty>No plans yet. Select routes and generate one.</Empty>}
          {plans.data && plans.data.length > 0 && (
            <ul className="list" data-testid="plan-list">
              {plans.data.map((p) => (
                <li key={p.id}>
                  <button type="button" className={`list-item ${p.id === planId ? 'list-item-active' : ''}`} onClick={() => setPlanId(p.id)} data-testid="plan-list-item">
                    <span className="list-title">{p.name}</span>
                    <Chip value={p.status} />
                    <span className="list-meta">
                      {p.summary?.samples ?? '—'} samples · {p.summary?.planned_views ?? '—'} views · {fmtTime(p.created_at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Section>

        {planId && (
          <Section
            title="Plan summary"
            testid="plan-summary"
            actions={pd ? <Chip value={pd.status} testid="plan-status" /> : undefined}
          >
            {plan.error && <ErrorList errors={plan.error} />}
            {!pd && !plan.error && <Loading what="plan" />}
            {pd && (
              <>
                <KV
                  rows={[
                    ['Name', pd.name],
                    ['Plan ID', <Mono key="id">{pd.id}</Mono>],
                    ['Segments', fmtNum(summary.segments)],
                    ['Samples', fmtNum(summary.samples)],
                    ['· street', fmtNum(summary.street_samples)],
                    ['· crossing approach', fmtNum(summary.crossing_approach_samples)],
                    ['· seed', fmtNum(summary.seed_samples)],
                    ['Planned views', fmtNum(summary.planned_views)],
                    ['Nodes', fmtNum(summary.nodes)],
                    ['Total length', summary.total_length_m !== undefined ? `${fmtNum(summary.total_length_m, 1)} m` : null],
                    ['CRS', summary.projected_crs ?? null],
                    ['Created', fmtTime(pd.created_at)],
                    ['Approved', pd.approved_at ? fmtTime(pd.approved_at) : null],
                    ['Config version', <Mono key="cv">{shortId(pd.config_version_id)}</Mono>],
                  ]}
                />
                {summary.warnings && summary.warnings.length > 0 && (
                  <Notice tone="warn">
                    <strong>Warnings</strong>
                    <ul>
                      {summary.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  </Notice>
                )}
                {summary.note && <p className="hint">{summary.note}</p>}
                {isDraft ? (
                  <div className="approve-box">
                    <button type="button" className="btn btn-primary" data-testid="approve-plan" onClick={() => void approve()} disabled={planAct.busy}>
                      {planAct.busy ? 'Approving…' : 'Approve plan'}
                    </button>
                    <span className="hint">Approval freezes the plan: samples can no longer be moved, excluded or edited. Jobs can only run from approved plans.</span>
                  </div>
                ) : (
                  <Notice tone="ok">
                    This plan is <strong>{pd.status}</strong> and immutable. To change samples or parameters, generate a new plan.
                  </Notice>
                )}
                <div className="row gap">
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => void deletePlan()} disabled={planAct.busy}>
                    Delete plan
                  </button>
                </div>
                <ErrorList errors={planAct.error} />
              </>
            )}
          </Section>
        )}

        {selSample && pd && (
          <SamplePanel
            sample={selSample}
            draft={isDraft}
            busy={sampleAct.busy}
            onPatch={patchSample}
            onClose={() => setSelectedSample(null)}
            onPan={() => {
              const c = selSample.geometry.coordinates as number[];
              panTo(c[1], c[0], 19);
            }}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- sub-panels

function RouteByCoords({ onAdd, busy }: { onAdd: (coords: [number, number][]) => Promise<unknown>; busy: boolean }) {
  const [text, setText] = useState('');
  const [errors, setErrors] = useState<string[] | null>(null);
  const add = async () => {
    const { coords, errors: errs } = parseLngLatPairs(text);
    if (errs.length) {
      setErrors(errs);
      return;
    }
    setErrors(null);
    const r = await onAdd(coords);
    if (r) setText('');
  };
  return (
    <details className="subsection" open>
      <summary>Route by coordinates</summary>
      <Field label={<>Paste <code>lng,lat</code> pairs — one per line or separated by <code>;</code></>} htmlFor="route-coords">
        <textarea
          id="route-coords"
          data-testid="route-coords"
          rows={3}
          className="mono"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={'151.20668,-33.87501\n151.20690,-33.87360'}
          spellCheck={false}
        />
      </Field>
      <ErrorList errors={errors} />
      <button type="button" className="btn btn-sm" data-testid="add-route-coords" onClick={() => void add()} disabled={busy}>
        Add route
      </button>
    </details>
  );
}

function ImportGeoJSON({ pid, onImported }: { pid: string; onImported: (rows: RouteRow[]) => Promise<void> }) {
  const [text, setText] = useState('');
  const [fileName, setFileName] = useState<string | null>(null);
  const act = useAction();
  const onFile = async (f: File | undefined) => {
    if (!f) return;
    setFileName(f.name);
    setText(await f.text());
  };
  const doImport = async () => {
    let obj: unknown;
    try {
      obj = JSON.parse(text);
    } catch (e) {
      act.setError([`Not valid JSON: ${e instanceof Error ? e.message : String(e)}`]);
      return;
    }
    if (!obj || typeof obj !== 'object') {
      act.setError(['GeoJSON must be an object (FeatureCollection, Feature or geometry)']);
      return;
    }
    const rows = await act.run(() => api.post<RouteRow[]>(`/api/projects/${pid}/routes/import`, obj));
    if (rows) {
      setText('');
      setFileName(null);
      await onImported(rows);
    }
  };
  return (
    <details className="subsection">
      <summary>Import GeoJSON</summary>
      <Field label="GeoJSON file" htmlFor="geojson-file">
        <input id="geojson-file" type="file" accept=".geojson,.json,application/geo+json,application/json" onChange={(e) => void onFile(e.target.files?.[0])} data-testid="import-geojson-file" />
      </Field>
      <Field label={fileName ? `Contents of ${fileName}` : 'or paste GeoJSON'} htmlFor="geojson-text" hint="Lines → import, polygons → polygon, points → Street View seeds. WGS84 lng/lat.">
        <textarea id="geojson-text" rows={3} className="mono" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} data-testid="import-geojson-text" />
      </Field>
      <ErrorList errors={act.error} />
      <button type="button" className="btn btn-sm" onClick={() => void doImport()} disabled={act.busy || !text.trim()} data-testid="import-geojson">
        {act.busy ? 'Importing…' : 'Import'}
      </button>
    </details>
  );
}

function StreetViewSeed({ onAdd, busy }: { onAdd: (p: ParsedStreetView, url: string) => Promise<void>; busy: boolean }) {
  const [url, setUrl] = useState('');
  const [parsed, setParsed] = useState<{ url: string; p: ParsedStreetView } | null>(null);
  const act = useAction();
  const parse = async () => {
    setParsed(null);
    const u = url.trim();
    if (!u) return;
    const p = await act.run(() => api.post<ParsedStreetView>('/api/streetview/parse', { url: u }));
    if (p) setParsed({ url: u, p });
  };
  const p = parsed?.p;
  return (
    <details className="subsection">
      <summary>Street View URL → starting observation</summary>
      <Field label="Street View URL" htmlFor="sv-url">
        <div className="row gap">
          <input id="sv-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.google.com/maps/@-33.87,151.20,3a,75y,90h,95t/…" data-testid="sv-url" />
          <button type="button" className="btn btn-sm" onClick={() => void parse()} disabled={act.busy || !url.trim()} data-testid="sv-parse">
            Parse
          </button>
        </div>
      </Field>
      <ErrorList errors={act.error} />
      {p && (
        <div className="sv-parsed" data-testid="sv-parsed">
          <KV
            rows={[
              ['lat, lng', `${fmtCoord(p.lat)}, ${fmtCoord(p.lng)}`],
              ['heading', fmtDeg(p.heading)],
              ['pitch', fmtDeg(p.pitch)],
              ['fov', fmtDeg(p.fov)],
              ['pano_id', p.pano_id ? <Mono key="p">{p.pano_id}</Mono> : null],
              ['provenance', p.provenance ?? null],
              ['URL form', p.url_form ?? null],
            ]}
          />
          {p.note && <p className="hint">{p.note}</p>}
          <button type="button" className="btn btn-sm" onClick={() => void onAdd(p, parsed!.url)} disabled={busy || p.lat === null || p.lng === null} data-testid="sv-add-seed">
            Add as starting observation
          </button>
        </div>
      )}
    </details>
  );
}

function SamplePanel({
  sample,
  draft,
  busy,
  onPatch,
  onClose,
  onPan,
}: {
  sample: Feature<SampleProps>;
  draft: boolean;
  busy: boolean;
  onPatch: (b: Record<string, unknown>) => Promise<void>;
  onClose: () => void;
  onPan: () => void;
}) {
  const p = sample.properties;
  const c = sample.geometry.coordinates as number[];
  const [note, setNote] = useState(p.note ?? '');
  useEffect(() => setNote(p.note ?? ''), [p.id, p.note]);
  return (
    <Section
      title={`Sample #${p.ordinal}`}
      testid="sample-panel"
      actions={
        <>
          <button type="button" className="btn btn-sm btn-ghost" onClick={onPan}>
            Zoom
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose} aria-label="Close sample details">
            ✕
          </button>
        </>
      }
    >
      <KV
        rows={[
          ['Kind', <Chip key="k" tone={p.kind === 'crossing_approach' ? 'purple' : p.kind === 'seed' ? 'warn' : 'neutral'}>{humanize(p.kind)}</Chip>],
          ['Segment', <Mono key="s">{p.segment_key ?? '—'}</Mono>],
          ['Seq in segment', p.seq_in_segment],
          ['Chainage', `${fmtNum(p.chainage_m, 2)} m`],
          ['Local bearing', fmtDeg(p.local_bearing_deg)],
          ['Requested position', `${fmtCoord(c[1])}, ${fmtCoord(c[0])}`],
          ['Node', p.node_key ? <Mono key="n">{p.node_key}</Mono> : null],
          ['Edited', p.edited ? 'yes (moved by operator)' : 'no'],
          ['Excluded', p.excluded ? <Chip key="e" value="excluded" /> : 'no'],
        ]}
      />
      <table className="table table-compact">
        <thead>
          <tr>
            <th>#</th>
            <th>Side</th>
            <th>View</th>
            <th className="right">Heading</th>
            <th className="right">Pitch</th>
            <th className="right">FOV</th>
          </tr>
        </thead>
        <tbody>
          {(p.views ?? []).map((v, i) => (
            <tr key={v.id}>
              <td>{i + 1}</td>
              <td>
                <span className="side-dot" style={{ background: SIDE_COLORS[v.side] ?? SIDE_COLORS.none }} /> {v.side}
              </td>
              <td title={v.basis}>{humanize(v.view_kind)}</td>
              <td className="right">{fmtDeg(v.heading_deg)}</td>
              <td className="right">{fmtDeg(v.pitch_deg)}</td>
              <td className="right">{fmtDeg(v.fov_deg)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {draft ? (
        <>
          <div className="row gap wrap">
            <button type="button" className="btn btn-sm" onClick={() => void onPatch({ excluded: !p.excluded })} disabled={busy} data-testid="sample-exclude">
              {p.excluded ? 'Include' : 'Exclude'}
            </button>
            <button type="button" className="btn btn-sm" onClick={() => void onPatch({ reset_position: true })} disabled={busy || !p.edited} data-testid="sample-reset">
              Reset position
            </button>
          </div>
          <Field label="Note" htmlFor="sample-note">
            <textarea id="sample-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          <button type="button" className="btn btn-sm" onClick={() => void onPatch({ note })} disabled={busy || note === (p.note ?? '')}>
            Save note
          </button>
        </>
      ) : (
        <>
          {p.note && <KV rows={[['Note', p.note]]} />}
          <p className="hint">Approved plans are immutable; sample edits are disabled.</p>
        </>
      )}
    </Section>
  );
}
