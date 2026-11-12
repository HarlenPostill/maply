import { useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import type { Atlas } from './context';
import { Legend } from './charts';
import { fmt, imageryAgeYears, photoCategory, segmentComposition, tally } from './model';

type Mode = 'photos' | 'segments';
type Height = 'uniform' | 'focus' | 'age' | 'notes' | 'confidence' | 'count' | 'share';
type Item = { key: number; x: number; z: number; color: string; height: number; selected: boolean };
type SceneState = {
  scene: THREE.Scene; camera: THREE.PerspectiveCamera; controls: OrbitControls; renderer: THREE.WebGLRenderer;
  content: THREE.Group; columns: THREE.InstancedMesh | null; keys: number[]; mode: Mode; fitKey: string;
  reset: () => void; render: () => void;
};
const HEIGHTS: Record<Mode, { key: Height; label: string; note: string }[]> = {
  photos: [
    { key: 'uniform', label: 'Uniform', note: 'Every photo the same height; colour carries the value.' },
    { key: 'focus', label: 'Highlighted value', note: 'Tall columns show the highlighted value; others stay low.' },
    { key: 'age', label: 'Imagery age', note: 'Height = years between imagery month and capture (max 15). Undated imagery stays flat.' },
    { key: 'notes', label: 'Review notes', note: 'Height = review notes on the photo (max 10).' },
    { key: 'confidence', label: 'Low confidence', note: 'Height = 100 − lowest model confidence (taller = less confident). Uncalibrated.' },
  ],
  segments: [
    { key: 'count', label: 'Photo count', note: 'Height = matching photos on the segment, relative to the busiest segment.' },
    { key: 'share', label: 'Share of highlighted value', note: 'Height = share of photos with the highlighted value among those with a known value.' },
  ],
};
const project = (lng: number, lat: number) => [(lng - 151.21) * 92300, -(lat + 33.88) * 111320] as const;

function disposeContent(group: THREE.Group) {
  group.traverse((item) => {
    if (item instanceof THREE.InstancedMesh) item.dispose();
    if (item instanceof THREE.Sprite) { item.material.map?.dispose(); item.material.dispose(); }
    if (item instanceof THREE.Mesh || item instanceof THREE.LineSegments) {
      item.geometry.dispose();
      (Array.isArray(item.material) ? item.material : [item.material]).forEach((m) => m.dispose());
    }
  });
  group.clear();
}

export default function Observation3D({ atlas }: { atlas: Atlas }) {
  const { ds, sel, colorBy, focus } = atlas;
  const host = useRef<HTMLDivElement>(null);
  const state = useRef<SceneState | null>(null);
  const latest = useRef(atlas); latest.current = atlas;
  const [error, setError] = useState('');
  const [mode, setMode] = useState<Mode>('photos');
  const [height, setHeight] = useState<Height>('uniform');
  const [scale, setScale] = useState(1);
  const [spread, setSpread] = useState(true);
  const focusIndex = focus ? colorBy.categories.findIndex((c) => c.key === focus) : -1;
  const heightMode = HEIGHTS[mode].some((h) => h.key === height) ? height : HEIGHTS[mode][0].key;

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true }); }
    catch { setError('3D rendering is unavailable in this browser. The map and charts contain the same observations.'); return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(renderer.domElement);
    renderer.domElement.tabIndex = 0;
    renderer.domElement.setAttribute('aria-label', '3D observations. Drag to orbit, scroll to zoom, right-drag to pan. Arrow keys rotate, plus and minus zoom, Home resets.');
    const scene = new THREE.Scene(); scene.background = new THREE.Color('#eef2f1');
    scene.fog = new THREE.Fog('#eef2f1', 9000, 26000);
    const camera = new THREE.PerspectiveCamera(42, 1, 1, 100000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.maxPolarAngle = Math.PI / 2.05; controls.minDistance = 30; controls.maxDistance = 40000;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x697b80, 2.4));
    const light = new THREE.DirectionalLight(0xffffff, 2.2); light.position.set(-1000, 4000, 2000); scene.add(light);
    const content = new THREE.Group(); scene.add(content);
    const render = () => renderer.render(scene, camera);
    const value: SceneState = { scene, camera, controls, renderer, content, columns: null, keys: [], mode: 'photos', fitKey: '', reset: () => {}, render };
    state.current = value;
    controls.addEventListener('change', render);
    const resize = new ResizeObserver(() => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h); camera.aspect = w / Math.max(1, h); camera.updateProjectionMatrix(); render();
    }); resize.observe(el);
    let down = { x: 0, y: 0 };
    const pointerDown = (e: PointerEvent) => { down = { x: e.clientX, y: e.clientY }; };
    const pointerUp = (e: PointerEvent) => {
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5 || !value.columns) return;
      const rect = renderer.domElement.getBoundingClientRect();
      const ray = new THREE.Raycaster();
      ray.setFromCamera(new THREE.Vector2((e.clientX - rect.left) / rect.width * 2 - 1, -(e.clientY - rect.top) / rect.height * 2 + 1), camera);
      const hit = ray.intersectObject(value.columns)[0];
      if (hit?.instanceId === undefined) return;
      const key = value.keys[hit.instanceId], a = latest.current;
      if (value.mode === 'photos') a.selectPhoto(key);
      else {
        const list: number[] = [];
        for (let i = 0; i < a.ds.n; i++) if (a.sel.photo[i] && a.ds.p.segment[i] === key) list.push(i);
        const segs = a.ds.snapshot.segments;
        a.selectGroup({ title: a.ds.snapshot.streets[segs.street[key]], subtitle: `Segment ${segs.key[key]}`, photos: list });
      }
    };
    const keyDown = (e: KeyboardEvent) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', 'Home'].includes(e.key)) return;
      e.preventDefault();
      if (e.key === 'Home') { value.reset(); return; }
      const sphere = new THREE.Spherical().setFromVector3(camera.position.clone().sub(controls.target));
      if (e.key === 'ArrowLeft') sphere.theta -= .12;
      if (e.key === 'ArrowRight') sphere.theta += .12;
      if (e.key === 'ArrowUp') sphere.phi = Math.max(.1, sphere.phi - .1);
      if (e.key === 'ArrowDown') sphere.phi = Math.min(Math.PI / 2.05, sphere.phi + .1);
      if (e.key === '+' || e.key === '=') sphere.radius *= .85;
      if (e.key === '-') sphere.radius *= 1.15;
      camera.position.copy(controls.target).add(new THREE.Vector3().setFromSpherical(sphere)); controls.update(); render();
    };
    renderer.domElement.addEventListener('pointerdown', pointerDown);
    renderer.domElement.addEventListener('pointerup', pointerUp);
    renderer.domElement.addEventListener('keydown', keyDown);
    return () => {
      resize.disconnect(); controls.dispose(); disposeContent(content); renderer.dispose(); renderer.forceContextLoss();
      renderer.domElement.remove(); state.current = null;
    };
  }, []);

  const items = useMemo((): Item[] => {
    const out: Item[] = [];
    if (mode === 'photos') {
      for (let i = 0; i < ds.n; i++) {
        if (!sel.photo[i]) continue;
        const k = photoCategory(colorBy, i), c = colorBy.categories[k];
        let [x, z] = project(ds.p.lng[i], ds.p.lat[i]);
        if (spread && ds.panoSize[i] > 1) {
          let hash = 0; for (const ch of ds.p.id[i]) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
          const angle = (hash % 3600) / 3600 * Math.PI * 2, radius = 2.5 + (hash % 400) / 100;
          x += Math.cos(angle) * radius; z += Math.sin(angle) * radius;
        }
        let h = .08;
        if (heightMode === 'uniform') h = c?.structural ? .05 : .35;
        else if (heightMode === 'focus') h = k === focusIndex ? 1 : .04;
        else if (heightMode === 'age') h = ds.p.imagery_month[i] >= 0 && ds.p.captured[i] >= 0 ? Math.min(1, Math.max(0, imageryAgeYears(ds.p.imagery_month[i], ds.p.captured[i])) / 15) : 0;
        else if (heightMode === 'notes') h = ds.p.review_reasons[i] > 0 ? Math.min(1, ds.p.review_reasons[i] / 10) : 0;
        else if (heightMode === 'confidence') h = ds.minConf[i] >= 0 ? Math.min(1, (100 - ds.minConf[i]) / 50) : 0;
        const dimmed = focusIndex >= 0 && heightMode !== 'focus' && k !== focusIndex;
        out.push({ key: i, x, z, color: dimmed ? '#c3cacd' : c?.color ?? '#999', height: h, selected: i === atlas.selectedPhoto });
      }
    } else {
      const comp = segmentComposition(ds, colorBy, sel), segs = ds.snapshot.segments;
      const max = Math.max(1, ...comp.photos);
      for (let s = 0; s < ds.segCount; s++) {
        if (!comp.photos[s] || segs.coordinates[s].length < 2) continue;
        const offset = s * comp.size;
        let best = -1, value = 0, known = 0;
        colorBy.categories.forEach((c, k) => { const v = comp.counts[offset + k]; if (!c.structural) { known += v; if (v > value) { value = v; best = k; } } });
        const coords = segs.coordinates[s], mid = coords[Math.floor(coords.length / 2)];
        const [x, z] = project((coords[0][0] + coords[coords.length - 1][0] + mid[0]) / 3, (coords[0][1] + coords[coords.length - 1][1] + mid[1]) / 3);
        const share = focusIndex >= 0 && known ? comp.counts[offset + focusIndex] / known : 0;
        out.push({ key: s, x, z, color: best >= 0 ? colorBy.categories[best].color : '#c5ccd0', height: heightMode === 'share' ? share : comp.photos[s] / max, selected: false });
      }
    }
    return out;
  }, [ds, sel, colorBy, mode, heightMode, focusIndex, spread, atlas.selectedPhoto]);

  useEffect(() => {
    const s = state.current;
    if (!s) return;
    disposeContent(s.content); s.columns = null; s.keys = [];
    const segs = ds.snapshot.segments, vertices: number[] = [];
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const item of items) { minX = Math.min(minX, item.x); maxX = Math.max(maxX, item.x); minZ = Math.min(minZ, item.z); maxZ = Math.max(maxZ, item.z); }
    const used = new Set<number>();
    for (let i = 0; i < ds.n; i++) if (sel.photo[i]) used.add(ds.p.segment[i]);
    for (const seg of used) {
      const c = segs.coordinates[seg];
      for (let k = 1; k < (c?.length ?? 0); k++) { const a = project(c[k - 1][0], c[k - 1][1]), b = project(c[k][0], c[k][1]); vertices.push(a[0], 0, a[1], b[0], 0, b[1]); }
    }
    if (!Number.isFinite(minX)) { s.render(); return; }
    const span = Math.max(maxX - minX, maxZ - minZ, 250), centre = new THREE.Vector3((minX + maxX) / 2, 0, (minZ + maxZ) / 2);
    s.content.add(new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3)), new THREE.LineBasicMaterial({ color: '#a7b5b6' })));
    const grid = new THREE.GridHelper(Math.ceil(span / 500) * 500 + 1000, Math.ceil(span / 500) + 2, '#c3cfce', '#dce4e2');
    grid.position.copy(centre); grid.position.y = -2; s.content.add(grid);
    const label = document.createElement('canvas'); label.width = 128; label.height = 128;
    const context = label.getContext('2d');
    if (context) {
      context.fillStyle = '#365b60'; context.font = 'bold 64px sans-serif'; context.textAlign = 'center'; context.fillText('N', 64, 90);
      const north = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(label) }));
      north.position.set(centre.x, span * .02, minZ - 300); north.scale.setScalar(span * .07); s.content.add(north);
    }
    const columns = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial({ roughness: .62 }), items.length);
    const transform = new THREE.Object3D(), color = new THREE.Color();
    const maxHeight = span * (mode === 'photos' ? .03 : .05) * scale;
    const width = mode === 'photos' ? Math.max(2, Math.min(6, span / 1200)) : Math.max(8, Math.min(22, span / 200));
    items.forEach((item, i) => {
      const h = Math.max(mode === 'photos' ? 1.5 : .6, item.height * maxHeight);
      transform.position.set(item.x, h / 2, item.z);
      const w = item.selected ? width * 2.4 : width;
      transform.scale.set(w, item.selected ? Math.max(h, maxHeight * .5) : h, w);
      transform.updateMatrix(); columns.setMatrixAt(i, transform.matrix);
      columns.setColorAt(i, color.set(item.selected ? '#12222b' : item.color));
    });
    columns.instanceMatrix.needsUpdate = true;
    s.content.add(columns); s.columns = columns; s.keys = items.map((item) => item.key); s.mode = mode;
    s.reset = () => {
      s.controls.target.copy(centre);
      s.camera.position.set(centre.x + span * .45, span * .8, centre.z + span * .95);
      s.controls.update(); s.render();
    };
    if (s.fitKey !== atlas.fitKey) { s.fitKey = atlas.fitKey; s.reset(); }
    s.render();
  }, [items, ds, sel, scale, mode, atlas.fitKey]);

  const legendTally = useMemo(() => tally(ds, colorBy, sel, 'photos'), [ds, colorBy, sel]);
  const heightInfo = HEIGHTS[mode].find((h) => h.key === heightMode)!;
  return <div className="viz-three-wrap">
    <div ref={host} className="viz-three" />
    {error ? <div className="viz-render-error" role="alert">{error}</div> : <>
      <div className="viz-three-help"><strong>3D observation landscape</strong><span>Drag to orbit · scroll to zoom · right-drag to pan · Home resets</span><span>{mode === 'photos' ? `${fmt(items.length)} photo columns, no sampling` : `${fmt(items.length)} segment columns`} · grid 500 m · N marks north</span></div>
      <div className="viz-three-controls">
        <div className="seg" role="group" aria-label="3D data points">{(['photos', 'segments'] as Mode[]).map((m) => <button key={m} type="button" aria-pressed={mode === m} onClick={() => setMode(m)}>{m === 'photos' ? 'Every photo' : 'Segments'}</button>)}</div>
        <label>Height <select aria-label="Height represents" value={heightMode} onChange={(e) => setHeight(e.target.value as Height)}>{HEIGHTS[mode].map((h) => <option key={h.key} value={h.key}>{h.label}</option>)}</select></label>
        {(heightMode === 'focus' || heightMode === 'share' || mode === 'photos') && <label>Highlight <select aria-label="3D highlight value" value={focus ?? ''} onChange={(e) => atlas.setFocus(e.target.value || null)}><option value="">None</option>{colorBy.categories.filter((c, k) => !c.structural && legendTally.counts[k]).map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}</select></label>}
        <label>Scale <input aria-label="Column height" type="range" min="0.2" max="3" step="0.1" value={scale} onChange={(e) => setScale(Number(e.target.value))} /><output>{scale.toFixed(1)}×</output></label>
        {mode === 'photos' && <label className="viz-check"><input type="checkbox" checked={spread} onChange={(e) => setSpread(e.target.checked)} />Separate photos sharing a panorama</label>}
        <small>{heightInfo.note}{(heightMode === 'focus' || heightMode === 'share') && focusIndex < 0 ? ' Choose a highlight value.' : ''} Heights are visual encodings, not terrain or obstacle heights.</small>
        <button type="button" className="btn" onClick={() => state.current?.reset()}>Reset camera</button>
      </div>
      <Legend title={`Colour: ${colorBy.label}`} categories={colorBy.categories} counts={legendTally.counts} selected={atlas.filters.cats[colorBy.id]}
        onToggle={(key, only) => atlas.toggle(colorBy.id, key, only)} note={mode === 'segments' ? 'Segment colour = most common known value.' : 'Click a column to inspect its source photo.'} />
    </>}
  </div>;
}
