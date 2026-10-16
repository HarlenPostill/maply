import { useEffect, useState, type RefObject } from 'react';
import L from './leafletGlobal';
import '@geoman-io/leaflet-geoman-free';
import 'leaflet/dist/leaflet.css';
import '@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css';
import type { Geometry } from './types';
import { destination } from './geo';

export { L };

export interface Basemap {
  url: string;
  attribution: string;
  max_zoom: number;
}

export const SYDNEY: [number, number] = [-33.8735, 151.207];
export const DEFAULT_ZOOM = 15;
const MAP_MAX_ZOOM = 21;

/**
 * Create a Leaflet map once for the given container. The basemap tile layer
 * comes from the backend (/api/system/status). An empty URL or "none" means
 * no tile layer at all (no external network use).
 */
export function useLeafletMap(ref: RefObject<HTMLDivElement | null>, basemap: Basemap | null | undefined): L.Map | null {
  const [map, setMap] = useState<L.Map | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const m = L.map(el, { center: SYDNEY, zoom: DEFAULT_ZOOM, maxZoom: MAP_MAX_ZOOM, zoomControl: true });
    m.attributionControl.setPrefix('Leaflet');
    L.control.scale({ imperial: false, position: 'bottomright' }).addTo(m);
    setMap(m);
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      m.remove();
      setMap(null);
    };
  }, [ref]);

  const url = basemap?.url?.trim() ?? '';
  const attribution = basemap?.attribution ?? '';
  const maxZoom = basemap?.max_zoom ?? 19;
  useEffect(() => {
    if (!map) return;
    if (!url || url.toLowerCase() === 'none') return;
    const layer = L.tileLayer(url, {
      attribution,
      maxNativeZoom: maxZoom,
      maxZoom: MAP_MAX_ZOOM,
      pmIgnore: true,
    });
    layer.addTo(map);
    layer.bringToBack();
    return () => {
      layer.remove();
    };
  }, [map, url, attribution, maxZoom]);

  return map;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type VectorLayer = L.Polyline<any>;

export function hasBasemap(basemap: Basemap | null | undefined): boolean {
  const url = basemap?.url?.trim() ?? '';
  return !!url && url.toLowerCase() !== 'none';
}

export function toLatLng(c: number[]): L.LatLng {
  return L.latLng(c[1], c[0]);
}

/** Build a vector layer for Line/Polygon geometries (Point handled by caller). */
export function geometryLayer(g: Geometry | null | undefined, opts: L.PolylineOptions): VectorLayer | null {
  if (!g || !g.coordinates) return null;
  const c = g.coordinates as unknown[];
  switch (g.type) {
    case 'LineString':
      return L.polyline(L.GeoJSON.coordsToLatLngs(c, 0), opts);
    case 'MultiLineString':
      return L.polyline(L.GeoJSON.coordsToLatLngs(c, 1), opts);
    case 'Polygon':
      return L.polygon(L.GeoJSON.coordsToLatLngs(c, 1), opts);
    case 'MultiPolygon':
      return L.polygon(L.GeoJSON.coordsToLatLngs(c, 2), opts);
    default:
      return null;
  }
}

/** Short ray from a point along a heading (drawing only). */
export function headingRay(lat: number, lng: number, heading: number, lengthM: number, opts: L.PolylineOptions): L.Polyline {
  const end = destination(lat, lng, heading, lengthM);
  return L.polyline(
    [
      [lat, lng],
      [end[0], end[1]],
    ],
    { interactive: false, pmIgnore: true, ...opts },
  );
}

export function divIcon(className: string, html: string, size: number): L.DivIcon {
  return L.divIcon({ className, html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

export function arrowIcon(bearingDeg: number): L.DivIcon {
  return L.divIcon({
    className: 'map-arrow-icon',
    html: `<div class="map-arrow" style="transform: rotate(${bearingDeg.toFixed(1)}deg)"></div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

export function diamondIcon(extraClass = ''): L.DivIcon {
  return L.divIcon({ className: 'map-diamond-icon', html: `<div class="map-diamond ${extraClass}"></div>`, iconSize: [12, 12], iconAnchor: [6, 6] });
}

export function labelIcon(text: string, className = 'map-label'): L.DivIcon {
  const esc = text.replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[ch] ?? ch);
  return L.divIcon({ className: 'map-label-icon', html: `<div class="${className}">${esc}</div>`, iconSize: [0, 0], iconAnchor: [0, 0] });
}

export function escapeHtml(s: string | null | undefined): string {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch] ?? ch);
}

/** Draw the approved project scope as a dashed outline with a label. */
export function scopeLayer(scope: Geometry | null | undefined): L.LayerGroup | null {
  const poly = geometryLayer(scope, {
    color: '#3d4a5c',
    weight: 2,
    dashArray: '8 6',
    fill: true,
    fillOpacity: 0.03,
    interactive: false,
    pmIgnore: true,
    snapIgnore: true,
  });
  if (!poly) return null;
  const g = L.layerGroup([poly], { pmIgnore: true });
  // Label at the northernmost vertex of the outer ring(s).
  let best: [number, number] | null = null;
  const visit = (c: unknown): void => {
    if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') {
      const pt = c as [number, number];
      if (!best || pt[1] > best[1]) best = pt;
    } else if (Array.isArray(c)) c.forEach(visit);
  };
  visit(scope?.coordinates);
  if (best) {
    const b = best as [number, number];
    L.marker([b[1], b[0]], { icon: labelIcon('approved scope (approximate)', 'map-label map-label-scope'), interactive: false, pmIgnore: true, keyboard: false }).addTo(g);
  }
  return g;
}

export function boundsOfGeometries(geoms: (Geometry | null | undefined)[]): L.LatLngBounds | null {
  let b: L.LatLngBounds | null = null;
  const visit = (c: unknown): void => {
    if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') {
      const ll = L.latLng(c[1] as number, c[0] as number);
      if (b) b.extend(ll);
      else b = L.latLngBounds(ll, ll);
    } else if (Array.isArray(c)) c.forEach(visit);
  };
  geoms.forEach((g) => g && visit(g.coordinates));
  return b;
}

export function fitTo(map: L.Map, b: L.LatLngBounds | null, maxZoom = 18): void {
  if (!b || !b.isValid()) return;
  const sw = b.getSouthWest();
  const ne = b.getNorthEast();
  if (sw.equals(ne)) map.setView(sw, Math.min(maxZoom, 18));
  else map.fitBounds(b, { padding: [40, 40], maxZoom });
}

export const SIDE_COLORS: Record<string, string> = { left: '#2563c9', right: '#e07b12', none: '#7a8494' };
