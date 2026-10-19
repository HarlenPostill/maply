import './leafletGlobal';
import L from 'leaflet';
import '@geoman-io/leaflet-geoman-free';
import { useEffect, useRef, useState } from 'react';
import type { FC, Geo, Status } from './api';

export type Cone = {
  id: string;
  lat: number;
  lng: number;
  heading: number;
  fov: number;
  tone: 'left' | 'right' | 'none' | 'done-left' | 'done-right' | 'done-none' | 'attention' | 'muted' | 'accepted';
  label: string;
};

export type DrawMode = 'area' | 'street' | null;

type Props = {
  status: Status;
  onViewChange?: (bbox: [number, number, number, number], zoom: number) => void;
  area: Geo | null;
  streets: FC | null;
  cones: Cone[];
  drawMode: DrawMode;
  onDrawn?: (geometry: Geo, mode: Exclude<DrawMode, null>) => void;
  onConeClick?: (id: string) => void;
  selectedId?: string | null;
  fitKey?: string;
  focusBounds?: [number, number, number, number] | null;
  focusKey?: number;
  taggingSegments?: FC | null;
  selectedSegment?: string | null;
  onSegmentClick?: (key: string) => void;
};

const SYDNEY: L.LatLngExpression = [-33.8735, 151.207];
const R_EARTH = 6378137;

function offset(lat: number, lng: number, bearing: number, metres: number): [number, number] {
  const b = (bearing * Math.PI) / 180;
  const dLat = (metres * Math.cos(b)) / R_EARTH;
  const dLng = (metres * Math.sin(b)) / (R_EARTH * Math.cos((lat * Math.PI) / 180));
  return [lat + (dLat * 180) / Math.PI, lng + (dLng * 180) / Math.PI];
}

function conePoints(c: Cone, radius: number): [number, number][] {
  const pts: [number, number][] = [[c.lat, c.lng]];
  const half = Math.min(c.fov, 100) / 2;
  for (let i = 0; i <= 8; i++) pts.push(offset(c.lat, c.lng, c.heading - half + (i * 2 * half) / 8, radius));
  return pts;
}

function bearing(a: number[], b: number[]): number {
  const [lng1, lat1, lng2, lat2] = [a[0], a[1], b[0], b[1]].map((v) => (v * Math.PI) / 180);
  const y = Math.sin(lng2 - lng1) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(lng2 - lng1);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

// Explicit styles (not CSS classes) so the canvas renderer can draw thousands of shots quickly.
const TONES: Record<Cone['tone'], L.PathOptions> = {
  left: { color: '#2f6fa8', fillColor: '#2f6fa8', fillOpacity: 0.18 },
  right: { color: '#a43f7e', fillColor: '#a43f7e', fillOpacity: 0.18 },
  none: { color: '#22313a', fillColor: '#22313a', fillOpacity: 0.15 },
  'done-left': { color: '#ffffff', fillColor: '#2f6fa8', fillOpacity: 0.85 },
  'done-right': { color: '#ffffff', fillColor: '#a43f7e', fillOpacity: 0.85 },
  'done-none': { color: '#ffffff', fillColor: '#22313a', fillOpacity: 0.8 },
  accepted: { color: '#ffffff', fillColor: '#2c7a4b', fillOpacity: 0.85 },
  attention: { color: '#22313a', fillColor: '#f2c230', fillOpacity: 0.95 },
  muted: { color: '#8a9499', fillColor: '#8a9499', fillOpacity: 0.35, dashArray: '2 2' },
};

export const TAG_COLORS: Record<string, string> = { unlabelled: '#94a3a5', queued: '#8663bc', running: '#246fa9', completed: '#2c7a4b', failed: '#bb4234', partial: '#bd8a17', unavailable: '#c5ccca' };

export default function MapView({ status, area, streets, cones, drawMode, onDrawn, onConeClick, selectedId, fitKey, onViewChange, taggingSegments, selectedSegment, onSegmentClick, focusBounds, focusKey }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layers = useRef<{ area: L.LayerGroup; streets: L.LayerGroup; cones: L.LayerGroup; tagging: L.LayerGroup } | null>(null);
  const drawnCb = useRef(onDrawn);
  drawnCb.current = onDrawn;
  const coneCb = useRef(onConeClick);
  coneCb.current = onConeClick;
  const segmentCb = useRef(onSegmentClick);
  segmentCb.current = onSegmentClick;
  const [zoom, setZoom] = useState(16);
  const viewCb = useRef(onViewChange);
  viewCb.current = onViewChange;

  useEffect(() => {
    if (!el.current) return;
    const m = L.map(el.current, { zoomControl: false, preferCanvas: true }).setView(SYDNEY, 16);
    L.control.zoom({ position: 'bottomright' }).addTo(m);
    if (status.basemap.url && status.basemap.url !== 'none') {
      L.tileLayer(status.basemap.url, { attribution: status.basemap.attribution, maxZoom: 20, maxNativeZoom: 19 }).addTo(m);
    }
    L.geoJSON(status.scope as any, { style: { color: '#22313A', weight: 1, dashArray: '4 6', fill: false, opacity: 0.5 }, interactive: false }).addTo(m);
    layers.current = { area: L.layerGroup().addTo(m), streets: L.layerGroup().addTo(m), tagging: L.layerGroup().addTo(m), cones: L.layerGroup().addTo(m) };
    m.pm.setGlobalOptions({ snappable: false, templineStyle: { color: '#22313A' }, hintlineStyle: { color: '#22313A', dashArray: '4 4' }, pathOptions: { color: '#22313A', weight: 2 } } as any);
    m.on('pm:create', (e: any) => {
      const geometry = e.layer.toGeoJSON().geometry as Geo;
      const mode = e.shape === 'Polygon' || e.shape === 'Rectangle' ? 'area' : 'street';
      m.removeLayer(e.layer);
      drawnCb.current?.(geometry, mode);
    });
    const report = () => {
      setZoom(m.getZoom());
      const b = m.getBounds();
      viewCb.current?.([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], m.getZoom());
    };
    m.on('moveend', report);
    setTimeout(report, 0);
    map.current = m;
    return () => {
      m.remove();
      map.current = null;
    };
  }, [status]);

  // drawing mode
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    m.pm.disableDraw();
    if (drawMode === 'area') m.pm.enableDraw('Polygon', { finishOn: null } as any);
    if (drawMode === 'street') m.pm.enableDraw('Line');
  }, [drawMode]);

  // area
  useEffect(() => {
    const g = layers.current?.area;
    if (!g) return;
    g.clearLayers();
    if (area) L.geoJSON(area as any, { style: { color: '#22313A', weight: 1.5, fillColor: '#F2C230', fillOpacity: 0.08 }, interactive: false }).addTo(g);
  }, [area]);

  // streets + direction marks
  useEffect(() => {
    const g = layers.current?.streets;
    if (!g) return;
    g.clearLayers();
    for (const f of streets?.features ?? []) {
      const on = f.properties.include !== false;
      const parts: number[][][] = f.geometry.type === 'MultiLineString' ? f.geometry.coordinates : [f.geometry.coordinates];
      for (const coords of parts) {
        L.polyline(coords.map((c) => [c[1], c[0]] as [number, number]), { color: on ? '#22313A' : '#9AA4A8', weight: on ? 3 : 2, opacity: on ? 0.55 : 0.5, dashArray: on ? undefined : '3 6', interactive: false }).addTo(g);
        if (on && coords.length > 1) {
          const mid = Math.floor((coords.length - 1) / 2);
          const a = coords[mid], b = coords[mid + 1];
          const icon = L.divIcon({ className: 'dir-mark', html: `<span style="transform:rotate(${bearing(a, b)}deg)"></span>`, iconSize: [14, 14] });
          L.marker([(a[1] + b[1]) / 2, (a[0] + b[0]) / 2], { icon, interactive: false, keyboard: false }).addTo(g);
        }
      }
    }
  }, [streets]);

  useEffect(() => {
    const g = layers.current?.tagging;
    if (!g) return;
    g.clearLayers();
    for (const f of taggingSegments?.features ?? []) {
      const p = f.properties;
      const selected = p.segment_key === selectedSegment;
      const line = L.geoJSON(f as any, { style: { color: TAG_COLORS[p.status] ?? TAG_COLORS.unlabelled, weight: selected ? 9 : 5, opacity: selected ? 1 : 0.82, dashArray: p.status === 'queued' ? '6 5' : undefined } });
      const tip = document.createElement('span');
      tip.textContent = `${p.street || 'Unnamed street'} · ${p.completed ?? 0}/${p.eligible ?? 0} tagged${p.needs_review ? ` · ${p.needs_review} need review` : ''}`;
      line.bindTooltip(tip, { sticky: true });
      line.on('click', () => segmentCb.current?.(p.segment_key));
      line.addTo(g);
    }
  }, [taggingSegments, selectedSegment]);

  // view cones
  useEffect(() => {
    const g = layers.current?.cones;
    if (!g) return;
    g.clearLayers();
    // Zoomed out: one quiet dot per shot. Zoomed in: a view cone showing where the camera looks.
    const close = zoom >= 18;
    for (const c of cones) {
      const sel = c.id === selectedId;
      const style: L.PathOptions = { ...TONES[c.tone], ...(sel ? { color: '#22313a', weight: 2.5 } : {}) };
      const shape = close
        ? L.polygon(conePoints(c, 9), { ...style, weight: style.weight ?? 1.25 })
        : L.circleMarker(offset(c.lat, c.lng, c.heading, 3), { ...style, radius: sel ? 5 : 2.5, weight: style.weight ?? 1 });
      shape.bindTooltip(c.label, { direction: 'top', sticky: true, className: 'cone-tip' });
      shape.on('click', () => coneCb.current?.(c.id));
      shape.addTo(g);
    }
  }, [cones, selectedId, zoom]);

  // fit to content when asked
  useEffect(() => {
    const m = map.current;
    if (!m || !fitKey) return;
    const bounds = L.latLngBounds([]);
    if (area) bounds.extend(L.geoJSON(area as any).getBounds());
    for (const f of streets?.features ?? []) bounds.extend(L.geoJSON(f as any).getBounds());
    for (const c of cones) bounds.extend([c.lat, c.lng]);
    if (bounds.isValid()) m.fitBounds(bounds, { padding: [48, 48], maxZoom: 18 });
  }, [fitKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!map.current || !focusBounds) return;
    const [west, south, east, north] = focusBounds;
    map.current.fitBounds([[south, west], [north, east]], { padding: [60, 60], maxZoom: 18 });
  }, [focusKey]); // Deliberately refit only on an explicit focus action, never a progress poll.

  return <div ref={el} className="map" aria-label="Map" />;
}
