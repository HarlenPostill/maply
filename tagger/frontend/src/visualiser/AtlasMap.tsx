import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import type { Atlas } from './context';
import { Legend } from './charts';
import { escapeHtml as esc } from './download';
import { fmt, gridCells, heat, percent, photoCategory, segmentComposition, tally, type Dataset, type Dimension } from './model';

type Mode = 'photos' | 'segments' | 'grid';

/** Draws every visible photo on one canvas; far faster than tens of thousands of Leaflet markers. */
class DotLayer extends L.Layer {
  private canvas = document.createElement('canvas');
  private host: L.Map | null = null;
  private groups: { color: string; faded: boolean; items: number[] }[] = [];
  private lat: ArrayLike<number> = [];
  private lng: ArrayLike<number> = [];
  private hash = new Map<number, number[]>();
  private xy = new Map<number, [number, number]>();
  selected: number | null = null;
  onAdd(map: L.Map) {
    this.host = map;
    this.canvas.className = 'viz-dot-layer leaflet-zoom-hide';
    map.getPane('overlayPane')!.appendChild(this.canvas);
    map.on('moveend zoomend resize viewreset', this.redraw, this);
    this.redraw();
    return this;
  }
  onRemove(map: L.Map) { this.canvas.remove(); map.off('moveend zoomend resize viewreset', this.redraw, this); this.host = null; return this; }
  setData(lat: ArrayLike<number>, lng: ArrayLike<number>, groups: { color: string; faded: boolean; items: number[] }[], selected: number | null) {
    this.lat = lat; this.lng = lng; this.groups = groups; this.selected = selected; this.redraw();
  }
  redraw() {
    const map = this.host;
    if (!map) return;
    const size = map.getSize(), ratio = Math.min(2, window.devicePixelRatio || 1);
    L.DomUtil.setPosition(this.canvas, map.containerPointToLayerPoint([0, 0]));
    this.canvas.width = size.x * ratio; this.canvas.height = size.y * ratio;
    this.canvas.style.width = `${size.x}px`; this.canvas.style.height = `${size.y}px`;
    const ctx = this.canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(ratio, ratio);
    const zoom = map.getZoom(), radius = zoom >= 18 ? 4.5 : zoom >= 16 ? 3.2 : zoom >= 14 ? 2.2 : 1.5;
    const bounds = map.getBounds().pad(.05);
    const west = bounds.getWest(), east = bounds.getEast(), south = bounds.getSouth(), north = bounds.getNorth();
    this.hash.clear(); this.xy.clear();
    for (const group of this.groups) {
      ctx.fillStyle = group.color;
      ctx.globalAlpha = group.faded ? .45 : .92;
      const r = group.faded ? Math.max(1.1, radius * .7) : radius;
      ctx.beginPath();
      for (const i of group.items) {
        const la = this.lat[i], ln = this.lng[i];
        if (la < south || la > north || ln < west || ln > east) continue;
        const p = map.latLngToContainerPoint([la, ln]);
        ctx.moveTo(p.x + r, p.y); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        const key = Math.floor(p.x / 10) * 100000 + Math.floor(p.y / 10);
        const bucket = this.hash.get(key);
        if (bucket) bucket.push(i); else this.hash.set(key, [i]);
        this.xy.set(i, [p.x, p.y]);
      }
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (this.selected !== null && this.xy.has(this.selected)) {
      const [x, y] = this.xy.get(this.selected)!;
      ctx.lineWidth = 3; ctx.strokeStyle = '#fff'; ctx.beginPath(); ctx.arc(x, y, radius + 5, 0, Math.PI * 2); ctx.stroke();
      ctx.lineWidth = 2; ctx.strokeStyle = '#12222b'; ctx.beginPath(); ctx.arc(x, y, radius + 5, 0, Math.PI * 2); ctx.stroke();
    }
  }
  hit(point: L.Point): number | null {
    let best: number | null = null, distance = 64;
    const cx = Math.floor(point.x / 10), cy = Math.floor(point.y / 10);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      for (const i of this.hash.get((cx + dx) * 100000 + cy + dy) ?? []) {
        const [x, y] = this.xy.get(i)!;
        const d = (x - point.x) ** 2 + (y - point.y) ** 2;
        if (d <= distance) { distance = d; best = i; }
      }
    }
    return best;
  }
}

function dominant(dim: Dimension, counts: Float64Array, offset: number) {
  let best = -1, value = 0, structural = -1, sValue = 0;
  dim.categories.forEach((c, k) => {
    const v = counts[offset + k];
    if (c.structural) { if (v > sValue) { sValue = v; structural = k; } } else if (v > value) { value = v; best = k; }
  });
  return { best, value, structural };
}

export function photoTitle(ds: Dataset, i: number, dim: Dimension) {
  const k = photoCategory(dim, i);
  return `${ds.p.code[i]} · ${ds.snapshot.streets[ds.p.street[i]]} · ${k >= 0 ? dim.categories[k].label : '—'}`;
}

export default function AtlasMap({ atlas }: { atlas: Atlas }) {
  const { ds, sel, colorBy, status, focus } = atlas;
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const shapes = useRef<L.LayerGroup | null>(null);
  const dots = useRef<DotLayer | null>(null);
  const fitted = useRef('');
  const latest = useRef(atlas); latest.current = atlas;
  const [mode, setMode] = useState<Mode>('photos');
  const [cell, setCell] = useState(100);
  const [hover, setHover] = useState<{ x: number; y: number; text: string } | null>(null);
  const modeRef = useRef(mode); modeRef.current = mode;

  useEffect(() => {
    if (!host.current) return;
    const m = L.map(host.current, { preferCanvas: true, zoomAnimation: false, fadeAnimation: false }).setView([-33.884, 151.205], 13);
    if (status.basemap.url) L.tileLayer(status.basemap.url, { attribution: status.basemap.attribution, maxZoom: 20, maxNativeZoom: 19, className: 'viz-tiles' }).addTo(m);
    L.control.scale({ imperial: false }).addTo(m);
    shapes.current = L.layerGroup().addTo(m);
    dots.current = new DotLayer().addTo(m);
    map.current = m;
    m.on('mousemove', (e: L.LeafletMouseEvent) => {
      if (modeRef.current !== 'photos') return;
      const i = dots.current?.hit(e.containerPoint) ?? null;
      host.current!.style.cursor = i === null ? '' : 'pointer';
      const a = latest.current;
      setHover(i === null ? null : { x: e.containerPoint.x, y: e.containerPoint.y, text: photoTitle(a.ds, i, a.colorBy) });
    });
    m.on('mouseout', () => setHover(null));
    m.on('click', (e: L.LeafletMouseEvent) => {
      if (modeRef.current !== 'photos') return;
      const i = dots.current?.hit(e.containerPoint) ?? null;
      if (i !== null) latest.current.selectPhoto(i);
    });
    const resize = new ResizeObserver(() => m.invalidateSize()); resize.observe(host.current);
    return () => { resize.disconnect(); m.remove(); map.current = null; shapes.current = null; dots.current = null; fitted.current = ''; };
  }, [status]);

  const legendTally = useMemo(() => tally(ds, colorBy, sel, 'photos'), [ds, colorBy, sel]);
  const focusIndex = focus ? colorBy.categories.findIndex((c) => c.key === focus) : -1;

  // Photo dots, grouped by colour so each colour is one canvas path.
  useEffect(() => {
    const layer = dots.current;
    if (!layer) return;
    if (mode !== 'photos') { layer.setData([], [], [], null); return; }
    const byCategory = new Map<number, number[]>();
    for (let i = 0; i < ds.n; i++) {
      if (!sel.photo[i]) continue;
      const k = photoCategory(colorBy, i);
      const list = byCategory.get(k);
      if (list) list.push(i); else byCategory.set(k, [i]);
    }
    const groups = [...byCategory].map(([k, items]) => {
      const c = colorBy.categories[k];
      const dimmed = focusIndex >= 0 && k !== focusIndex;
      return { color: dimmed ? '#b8c0c4' : c?.color ?? '#999', faded: !c || c.structural || dimmed, items, order: !c || c.structural ? 0 : dimmed ? 1 : 2 + items.length ** -1 };
    }).sort((a, b) => a.order - b.order);
    layer.setData(ds.p.lat, ds.p.lng, groups, atlas.selectedPhoto);
  }, [ds, sel, colorBy, mode, focusIndex, atlas.selectedPhoto]);

  // Segment lines and grid cells.
  useEffect(() => {
    const group = shapes.current;
    if (!group) return;
    group.clearLayers();
    if (mode === 'segments') {
      const comp = segmentComposition(ds, colorBy, sel);
      const segs = ds.snapshot.segments;
      for (let s = 0; s < ds.segCount; s++) {
        const photos = comp.photos[s];
        if (!photos || segs.coordinates[s].length < 2) continue;
        const offset = s * comp.size, d = dominant(colorBy, comp.counts, offset);
        let known = 0;
        colorBy.categories.forEach((c, k) => { if (!c.structural) known += comp.counts[offset + k]; });
        const share = focusIndex >= 0 && known ? comp.counts[offset + focusIndex] / known : null;
        const color = focusIndex >= 0 ? (share === null ? '#c5ccd0' : heat(.15 + share * .85)) : d.best >= 0 ? colorBy.categories[d.best].color : '#c5ccd0';
        const line = L.polyline(segs.coordinates[s].map(([lng, lat]) => [lat, lng] as L.LatLngTuple), { color, weight: 4.5, opacity: known ? .9 : .5 });
        line.bindTooltip(() => {
          const parts = colorBy.categories.map((c, k) => ({ c, v: comp.counts[offset + k] })).filter((x) => x.v).sort((a, b) => b.v - a.v).slice(0, 4);
          return `${esc(ds.snapshot.streets[segs.street[s]])} · ${fmt(photos)} photos${share !== null ? ` · ${percent(share)} ${esc(colorBy.categories[focusIndex].label.toLowerCase())}` : ''}<br>${parts.map((x) => `${esc(x.c.label)}: ${fmt(x.v)}`).join('<br>')}`;
        }, { sticky: true });
        line.on('click', () => {
          const list: number[] = [];
          for (let i = 0; i < ds.n; i++) if (sel.photo[i] && ds.p.segment[i] === s) list.push(i);
          latest.current.selectGroup({ title: ds.snapshot.streets[segs.street[s]], subtitle: `Segment ${segs.key[s]} · ${Math.round(segs.length_m[s])} m`, photos: list });
        });
        line.addTo(group);
      }
    } else if (mode === 'grid') {
      const cells = gridCells(ds, colorBy, sel, cell);
      const max = Math.max(1, ...cells.map((c) => c.photos.length));
      for (const c of cells) {
        const d = dominant(colorBy, c.counts, 0);
        let known = 0;
        colorBy.categories.forEach((cat, k) => { if (!cat.structural) known += c.counts[k]; });
        const share = focusIndex >= 0 && known ? c.counts[focusIndex] / known : null;
        const color = focusIndex >= 0 ? (share === null ? '#c5ccd0' : heat(.1 + share * .9)) : d.best >= 0 ? colorBy.categories[d.best].color : '#c5ccd0';
        const rect = L.rectangle([[c.south, c.west], [c.north, c.east]], { color: '#fff', weight: .6, fillColor: color, fillOpacity: focusIndex >= 0 ? .78 : .25 + .6 * Math.sqrt(c.photos.length / max) });
        rect.bindTooltip(() => {
          const parts = colorBy.categories.map((cat, k) => ({ cat, v: c.counts[k] })).filter((x) => x.v).sort((a, b) => b.v - a.v).slice(0, 5);
          return `${fmt(c.photos.length)} photos${share !== null ? ` · ${percent(share)} ${esc(colorBy.categories[focusIndex].label.toLowerCase())} of ${fmt(known)} known` : ''}<br>${parts.map((x) => `${esc(x.cat.label)}: ${fmt(x.v)}`).join('<br>')}`;
        }, { sticky: true });
        rect.on('click', () => latest.current.selectGroup({ title: `${cell} m grid cell`, subtitle: `${fmt(c.photos.length)} photos`, photos: c.photos }));
        rect.addTo(group);
      }
    }
  }, [ds, sel, colorBy, mode, cell, focusIndex]);

  // Fit to the current selection when the fit key changes.
  useEffect(() => {
    const m = map.current;
    if (!m || fitted.current === atlas.fitKey) return;
    let w = 180, e = -180, s = 90, n = -90, any = false;
    for (let i = 0; i < ds.n; i++) if (sel.photo[i]) { any = true; w = Math.min(w, ds.p.lng[i]); e = Math.max(e, ds.p.lng[i]); s = Math.min(s, ds.p.lat[i]); n = Math.max(n, ds.p.lat[i]); }
    if (any) { m.fitBounds([[s, w], [n, e]], { padding: [30, 30], maxZoom: 18, animate: false }); fitted.current = atlas.fitKey; }
  }, [atlas.fitKey, ds, sel]);

  useEffect(() => {
    const m = map.current, i = atlas.selectedPhoto;
    if (m && i !== null && !m.getBounds().contains([ds.p.lat[i], ds.p.lng[i]])) m.panTo([ds.p.lat[i], ds.p.lng[i]], { animate: false });
  }, [atlas.selectedPhoto, ds]);

  return <div className="viz-map-wrap">
    <div className="viz-map" ref={host} aria-label={`Map of ${fmt(sel.photos)} photos coloured by ${colorBy.label}`} />
    {hover && <div className="viz-map-hover" style={{ left: hover.x + 14, top: hover.y + 10 }}>{hover.text}</div>}
    <div className="viz-map-controls">
      <div className="seg" role="group" aria-label="Map layer">{(['photos', 'segments', 'grid'] as Mode[]).map((m) => <button key={m} type="button" aria-pressed={mode === m} onClick={() => setMode(m)}>{m === 'photos' ? 'Photos' : m === 'segments' ? 'Segments' : 'Grid'}</button>)}</div>
      {mode === 'grid' && <label>Cell <select aria-label="Grid cell size" value={cell} onChange={(e) => setCell(Number(e.target.value))}>{[50, 100, 200, 400].map((n) => <option key={n} value={n}>{n} m</option>)}</select></label>}
      <label>Highlight <select aria-label="Highlight value" value={focus ?? ''} onChange={(e) => atlas.setFocus(e.target.value || null)}>
        <option value="">{mode === 'photos' ? 'All values' : 'Most common value'}</option>
        {colorBy.categories.filter((c, k) => !c.structural && legendTally.counts[k] > 0).map((c) => <option key={c.key} value={c.key}>{mode === 'photos' ? c.label : `Share: ${c.label}`}</option>)}
      </select></label>
    </div>
    <Legend title={`${colorBy.label} · ${fmt(sel.photos)} photos`} categories={colorBy.categories} counts={legendTally.counts}
      selected={atlas.filters.cats[colorBy.id]} onToggle={(key, only) => atlas.toggle(colorBy.id, key, only)}
      note={mode === 'photos'
        ? `${colorBy.level === 'feature' ? 'Photos with several values show their rarest one. ' : ''}Camera positions (planned where unavailable), not surveyed feature locations. Click a value to filter.`
        : focusIndex >= 0 ? `Darker = larger share of ${colorBy.categories[focusIndex].label.toLowerCase()} among photos with a known value. Grey = no known value.`
          : `Colour = most common known value ${mode === 'grid' ? 'in each cell; stronger = more photos' : 'on each segment'}. Choose a highlight for shares.`} />
  </div>;
}
