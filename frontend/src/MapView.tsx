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
  area: Geo | null;
  streets: FC | null;
  cones: Cone[];
  drawMode: DrawMode;
  onDrawn?: (geometry: Geo, mode: Exclude<DrawMode, null>) => void;
  onConeClick?: (id: string) => void;
  selectedId?: string | null;
  fitKey?: string;
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

export default function MapView({ status, area, streets, cones, drawMode, onDrawn, onConeClick, selectedId, fitKey }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layers = useRef<{ area: L.LayerGroup; streets: L.LayerGroup; cones: L.LayerGroup } | null>(null);
  const drawnCb = useRef(onDrawn);
  drawnCb.current = onDrawn;
  const coneCb = useRef(onConeClick);
  coneCb.current = onConeClick;
  const [zoom, setZoom] = useState(16);

  useEffect(() => {
    if (!el.current) return;
    const m = L.map(el.current, { zoomControl: false, preferCanvas: false }).setView(SYDNEY, 16);
    L.control.zoom({ position: 'bottomright' }).addTo(m);
    if (status.basemap.url && status.basemap.url !== 'none') {
      L.tileLayer(status.basemap.url, { attribution: status.basemap.attribution, maxZoom: 20, maxNativeZoom: 19 }).addTo(m);
    }
    L.geoJSON(status.scope as any, { style: { color: '#22313A', weight: 1, dashArray: '4 6', fill: false, opacity: 0.5 }, interactive: false }).addTo(m);
    layers.current = { area: L.layerGroup().addTo(m), streets: L.layerGroup().addTo(m), cones: L.layerGroup().addTo(m) };
    m.pm.setGlobalOptions({ snappable: false, templineStyle: { color: '#22313A' }, hintlineStyle: { color: '#22313A', dashArray: '4 4' }, pathOptions: { color: '#22313A', weight: 2 } } as any);
    m.on('pm:create', (e: any) => {
      const geometry = e.layer.toGeoJSON().geometry as Geo;
      const mode = e.shape === 'Polygon' || e.shape === 'Rectangle' ? 'area' : 'street';
      m.removeLayer(e.layer);
      drawnCb.current?.(geometry, mode);
    });
    m.on('zoomend', () => setZoom(m.getZoom()));
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

  // view cones
  useEffect(() => {
    const g = layers.current?.cones;
    if (!g) return;
    g.clearLayers();
    // Zoomed out: one quiet dot per shot. Zoomed in: a view cone showing where the camera looks.
    const close = zoom >= 18;
    for (const c of cones) {
      const sel = c.id === selectedId;
      const cls = `cone cone-${c.tone}${sel ? ' cone-selected' : ''}`;
      const shape = close
        ? L.polygon(conePoints(c, 9), { className: cls, weight: sel ? 2.5 : 1.25 })
        : L.circleMarker(offset(c.lat, c.lng, c.heading, 3), { className: cls, radius: sel ? 5 : 2.5, weight: sel ? 2 : 1 });
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

  return <div ref={el} className="map" aria-label="Map" />;
}
