import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import type { Status } from '../api';
import { heatCells, pointColour, pointDescription, pointLegend, pointSignal, type Metric, type PhotoPoint } from './data';

export default function PhotoMap({ points, metric, status, selected, onSelect, fitKey, heatmap = false }: {
  points: PhotoPoint[]; metric: Metric; status: Status; selected: string | null;
  onSelect: (id: string) => void; fitKey: string; heatmap?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const select = useRef(onSelect); select.current = onSelect;
  const fitted = useRef('');
  const [size, setSize] = useState(100);
  const [mode, setMode] = useState<'rate' | 'count'>('rate');
  const [minimum, setMinimum] = useState(1);
  const [cellKey, setCellKey] = useState('');
  const cells = useMemo(() => heatCells(points, metric, size), [points, metric, size]);
  const cell = cells.find((item) => item.key === cellKey);
  const maxCount = Math.max(1, ...cells.filter((item) => item.observed >= minimum).map((item) => item.flagged));
  const displayMode = metric === 'coverage' ? 'count' : mode;
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
    const m = map.current, group = layer.current;
    if (!m || !group) return;
    group.clearLayers();
    const bounds = L.latLngBounds(points.map((p): L.LatLngTuple => [p.coordinates[1], p.coordinates[0]]));
    if (heatmap) {
      for (const cell of cells) {
        const rate = displayMode === 'rate' ? cell.flagged / Math.max(1, cell.observed) : cell.flagged / maxCount;
        const known = cell.observed >= minimum;
        const color = !known ? '#a7b2b8' : rate === 0 ? '#25857b' : rate < .25 ? '#e2bd61' : rate < .5 ? '#db884f' : '#b94749';
        const rect = L.rectangle([[cell.south, cell.west], [cell.north, cell.east]], { color: '#fff', weight: 1, fillColor: color, fillOpacity: known ? .72 : .28 });
        const tip = document.createElement('span');
        tip.textContent = `${cell.flagged} signal photos / ${cell.observed} assessed · ${cell.points.length} total tagged photos${known ? '' : ' · insufficient evidence'}`;
        rect.bindTooltip(tip, { sticky: true }).on('click', () => setCellKey(cell.key)).addTo(group);
      }
    } else {
      const ordered = [...points].sort((a, b) => pointSignal(a, metric) - pointSignal(b, metric));
      for (const point of ordered) {
        const unknown = pointSignal(point, metric) === -1 && !(metric === 'quality' && point.metrics.footpath === 1);
        const marker = L.circleMarker([point.coordinates[1], point.coordinates[0]], { radius: selected === point.id ? 7 : unknown ? 2 : 3.5, color: selected === point.id ? '#102f3e' : pointColour(point, metric), fillColor: pointColour(point, metric), fillOpacity: unknown ? .3 : .9, weight: selected === point.id ? 2 : .5 });
        const tip = document.createElement('span'); tip.textContent = `${point.code} · ${point.street} · ${pointDescription(point, metric)}`;
        marker.bindTooltip(tip, { sticky: true }).on('click', () => select.current(point.id)).addTo(group);
      }
    }
    if (fitted.current !== fitKey && bounds.isValid()) { m.fitBounds(bounds, { padding: [35, 35], maxZoom: 18, animate: false }); fitted.current = fitKey; }
  }, [points, cells, metric, selected, fitKey, heatmap, displayMode, minimum, maxCount]);
  return <>
    <div className="viz-map" ref={host} aria-label={heatmap ? 'Photo observation grid heatmap' : 'Individual footpath photo observations'} />
    {heatmap && <div className="viz-heat-controls">
      <label>Grid size <select aria-label="Heatmap grid size" value={size} onChange={(e) => { setSize(Number(e.target.value)); setCellKey(''); }}>{[50, 100, 200].map((n) => <option key={n} value={n}>{n} m</option>)}</select></label>
      <label>Intensity <select aria-label="Heatmap intensity" value={displayMode} disabled={metric === 'coverage'} onChange={(e) => { if (e.target.value === 'rate' || e.target.value === 'count') setMode(e.target.value); }}><option value="rate">Concern rate</option><option value="count">Photo count</option></select></label>
      <label>Minimum assessed <select aria-label="Heatmap minimum observations" value={minimum} onChange={(e) => setMinimum(Number(e.target.value))}>{[1, 5, 10, 20].map((n) => <option key={n} value={n}>{n} photos</option>)}</select></label>
      {cell && <div className="viz-cell-detail"><strong>{cell.flagged} signal / {cell.observed} assessed</strong><span>{cell.points.length} tagged photos in this cell</span><select aria-label="Inspect heatmap photo" value={cell.points.some((p) => p.id === selected) ? selected ?? '' : ''} onChange={(e) => { if (e.target.value) onSelect(e.target.value); }}><option value="">Inspect a source photo…</option>{cell.points.map((point) => <option key={point.id} value={point.id}>{point.code} · {pointDescription(point, metric)}</option>)}</select><button className="viz-text-button" onClick={() => setCellKey('')}>Close cell</button></div>}
    </div>}
    <div className="viz-legend"><strong>{heatmap ? `${size} m grid · ${displayMode === 'rate' ? 'Signal photos / assessed photos' : `Signal photo count · maximum ${maxCount}`}` : `${points.length.toLocaleString()} individual tagged photos · no sampling`}</strong>
      <div>{(heatmap ? [['#a7b2b8', 'Too few / unknown'], ['#25857b', 'Zero signals'], ['#e2bd61', displayMode === 'rate' ? '>0–<25%' : '<25% of max'], ['#db884f', displayMode === 'rate' ? '25–<50%' : '25–<50% of max'], ['#b94749', displayMode === 'rate' ? '50–100%' : '50–100% of max']] : pointLegend(metric)).map(([color, label]) => <span key={label}><i style={{ background: color }} />{label}</span>)}</div>
      <small>{heatmap ? `Select a cell to inspect all its photos. ${metric === 'quality' ? 'Unknown quality and missing footpaths are excluded from quality rates. ' : ''}Counts reflect capture density, not unique defects.` : 'Camera locations (planned positions where unavailable), not surveyed footpath coordinates. Overlapping photos remain separate records.'}</small>
    </div>
  </>;
}
