import { useEffect, useRef } from 'react';
import L from 'leaflet';
import type { Status } from '../api';
import { colour, measure, percent, type Metric, type Segment } from './data';

export default function ObservationMap({ rows, metric, status, selected, onSelect, fitKey }: {
  rows: Segment[]; metric: Metric; status: Status; selected: string | null; onSelect: (key: string) => void; fitKey: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const select = useRef(onSelect); select.current = onSelect;
  const fitted = useRef('');
  useEffect(() => {
    if (!host.current) return;
    const m = L.map(host.current, { preferCanvas: true, zoomAnimation: false }).setView([-33.884, 151.205], 13);
    if (status.basemap.url) L.tileLayer(status.basemap.url, { attribution: status.basemap.attribution, maxZoom: 20, maxNativeZoom: 19 }).addTo(m);
    L.control.scale({ imperial: false }).addTo(m);
    map.current = m; layer.current = L.layerGroup().addTo(m);
    const resize = new ResizeObserver(() => m.invalidateSize()); resize.observe(host.current);
    return () => { resize.disconnect(); m.remove(); map.current = null; layer.current = null; fitted.current = ''; };
  }, [status]);
  useEffect(() => {
    const group = layer.current, m = map.current;
    if (!group || !m) return;
    group.clearLayers();
    const bounds = L.latLngBounds([]);
    for (const row of rows) {
      const coords: L.LatLngTuple[] = row.coordinates.map(([lng, lat]) => [lat, lng]);
      coords.forEach((coord) => bounds.extend(coord));
      const line = L.polyline(coords, { color: colour(row, metric), weight: row.key === selected ? 9 : 5,
        opacity: row.key === selected ? 1 : .85, dashArray: row.tagged ? undefined : '3 6' });
      const tip = document.createElement('span');
      const value = measure(row, metric);
      tip.textContent = `${row.street} · ${value.numerator}/${value.denominator} photos (${percent(value.rate)}) · ${row.tagged}/${row.planned} tagged`;
      line.bindTooltip(tip, { sticky: true }).on('click', () => select.current(row.key)).addTo(group);
      if (row.key === selected) line.bringToFront();
    }
    if (fitted.current !== fitKey && bounds.isValid()) { m.fitBounds(bounds, { padding: [30, 30], maxZoom: 17, animate: false }); fitted.current = fitKey; }
  }, [rows, metric, selected, fitKey]);
  return <div className="viz-map" ref={host} aria-label="Accessibility observations by planned street segment" />;
}
