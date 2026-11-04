import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { colour, measure, pointColour, pointSignal, pointLegend, type PhotoPoint, type Metric, type Segment } from './data';

type SceneState = {
  scene: THREE.Scene; camera: THREE.PerspectiveCamera; controls: OrbitControls;
  renderer: THREE.WebGLRenderer; content: THREE.Group;
  columns: THREE.InstancedMesh | null; keys: string[]; fitKey: string; mode: 'photos' | 'segments';
  reset: () => void; render: () => void;
};
function disposeContent(group: THREE.Group) {
  group.traverse((item) => {
    if (item instanceof THREE.InstancedMesh) item.dispose();
    if (item instanceof THREE.Sprite) { item.material.map?.dispose(); item.material.dispose(); }
    if (item instanceof THREE.Mesh || item instanceof THREE.LineSegments) {
      item.geometry.dispose();
      const materials = Array.isArray(item.material) ? item.material : [item.material];
      materials.forEach((material) => material.dispose());
    }
  });
  group.clear();
}
export default function Observation3D({ rows, points, metric, selected, selectedPhoto, onSelect, onPhotoSelect, fitKey }: {
  points: PhotoPoint[]; selectedPhoto: string | null; onPhotoSelect: (id: string) => void;
  rows: Segment[]; metric: Metric; selected: string | null; onSelect: (key: string) => void; fitKey: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const state = useRef<SceneState | null>(null);
  const select = useRef(onSelect); select.current = onSelect;
  const selectPhoto = useRef(onPhotoSelect); selectPhoto.current = onPhotoSelect;
  const [error, setError] = useState('');
  const [heightScale, setHeightScale] = useState(1);
  const [minimum, setMinimum] = useState(1);
  const [mode, setMode] = useState<'photos' | 'segments'>('photos');
  const [spread, setSpread] = useState(true);
  const [heightMode, setHeightMode] = useState<'rate' | 'count'>('rate');
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true }); }
    catch { setError('3D rendering is unavailable in this browser. The map and street comparisons contain the same observations.'); return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(renderer.domElement);
    renderer.domElement.tabIndex = 0;
    renderer.domElement.setAttribute('aria-label', '3D observations. Drag to orbit, scroll to zoom, right-drag to pan. Arrow keys rotate, plus and minus zoom.');
    const scene = new THREE.Scene(); scene.background = new THREE.Color('#edf1f0');
    const camera = new THREE.PerspectiveCamera(42, 1, 1, 100000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.maxPolarAngle = Math.PI / 2.05;
    controls.minDistance = 40; controls.maxDistance = 40000;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x697b80, 2.5));
    const light = new THREE.DirectionalLight(0xffffff, 2.5); light.position.set(-1000, 4000, 2000); scene.add(light);
    const content = new THREE.Group(); scene.add(content);
    const render = () => renderer.render(scene, camera);
    const value: SceneState = { scene, camera, controls, renderer, content, columns: null, keys: [], fitKey: '', mode: 'photos', reset: () => {}, render };
    state.current = value;
    controls.addEventListener('change', render);
    const resize = new ResizeObserver(() => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h); camera.aspect = w / Math.max(1, h); camera.updateProjectionMatrix(); render();
    }); resize.observe(el);
    let down = { x: 0, y: 0 };
    const pointerDown = (event: PointerEvent) => { down = { x: event.clientX, y: event.clientY }; };
    const pointerUp = (event: PointerEvent) => {
      if (Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5 || !value.columns) return;
      const rect = renderer.domElement.getBoundingClientRect();
      const ray = new THREE.Raycaster();
      ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), camera);
      const hit = ray.intersectObject(value.columns)[0];
      if (hit?.instanceId !== undefined) {
        if (value.mode === 'photos') selectPhoto.current(value.keys[hit.instanceId]);
        else select.current(value.keys[hit.instanceId]);
      }
    };
    const keyDown = (event: KeyboardEvent) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', 'Home'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'Home') { value.reset(); return; }
      const offset = camera.position.clone().sub(controls.target);
      const sphere = new THREE.Spherical().setFromVector3(offset);
      if (event.key === 'ArrowLeft') sphere.theta -= .12;
      if (event.key === 'ArrowRight') sphere.theta += .12;
      if (event.key === 'ArrowUp') sphere.phi = Math.max(.1, sphere.phi - .1);
      if (event.key === 'ArrowDown') sphere.phi = Math.min(Math.PI / 2.05, sphere.phi + .1);
      if (event.key === '+' || event.key === '=') sphere.radius *= .85;
      if (event.key === '-') sphere.radius *= 1.15;
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
  useEffect(() => {
    const s = state.current;
    if (!s) return;
    disposeContent(s.content); s.columns = null; s.keys = [];
    if (!rows.length) { s.render(); return; }
    // A fixed Sydney origin keeps camera position stable as streamed data changes.
    const project = ([lng, lat]: [number, number]) => new THREE.Vector3((lng - 151.21) * 92300, 0, -(lat + 33.88) * 111320);
    const bounds = new THREE.Box3();
    const vertices: number[] = [];
    for (const row of rows) {
      row.coordinates.forEach((coord) => bounds.expandByPoint(project(coord)));
      for (let i = 1; i < row.coordinates.length; i++) {
        vertices.push(...project(row.coordinates[i - 1]).toArray(), ...project(row.coordinates[i]).toArray());
      }
    }
    const size = bounds.getSize(new THREE.Vector3()); const span = Math.max(size.x, size.z, 250);
    const centre = bounds.getCenter(new THREE.Vector3());
    const lines = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3)), new THREE.LineBasicMaterial({ color: '#9dadae' }));
    s.content.add(lines);
    const grid = new THREE.GridHelper(Math.ceil(span / 500) * 500 + 1000, Math.ceil(span / 500) + 2, '#b9c8c7', '#d8e1df');
    grid.position.copy(centre); grid.position.y = -2; s.content.add(grid);
    const label = document.createElement('canvas'); label.width = 128; label.height = 128;
    const context = label.getContext('2d');
    if (context) {
      context.fillStyle = '#365b60'; context.font = 'bold 64px sans-serif'; context.textAlign = 'center'; context.fillText('N', 64, 90);
      const north = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(label) }));
      north.position.set(centre.x, span * .02, bounds.min.z - 300); north.scale.setScalar(span * .07); s.content.add(north);
    }
    const visibleRows = rows.filter((row) => measure(row, metric).denominator >= minimum);
    const maximumCount = Math.max(1, ...visibleRows.map((row) => measure(row, metric).numerator));
    const duplicates = new Map<string, number>();
    const locationKey = (point: PhotoPoint) => point.coordinates.map((v) => v.toFixed(6)).join(':');
    for (const point of points) duplicates.set(locationKey(point), (duplicates.get(locationKey(point)) ?? 0) + 1);
    const items = mode === 'photos' ? points.map((point) => {
      const position = project(point.coordinates);
      if (spread && (duplicates.get(locationKey(point)) ?? 0) > 1) {
        let hash = 0; for (const char of point.id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
        const angle = (hash % 3600) / 3600 * Math.PI * 2;
        const radius = 3 + (hash % 500) / 100;
        position.x += Math.cos(angle) * radius; position.z += Math.sin(angle) * radius;
      }
      return { key: point.id, position, height: Math.max(0, pointSignal(point, metric)), color: pointColour(point, metric), selected: point.id === selectedPhoto };
    }) : visibleRows.map((row) => ({
      key: row.key, position: project(row.coordinates[0]).add(project(row.coordinates[row.coordinates.length - 1])).multiplyScalar(.5),
      height: heightMode === 'rate' ? measure(row, metric).rate ?? 0 : measure(row, metric).numerator / maximumCount,
      color: colour(row, metric), selected: row.key === selected,
    }));
    const columns = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial({ roughness: .65 }), items.length);
    const transform = new THREE.Object3D();
    const maxHeight = span * .025 * heightScale;
    items.forEach((item, i) => {
      const height = Math.max(mode === 'photos' ? 2 : .5, item.height * maxHeight);
      transform.position.set(item.position.x, height / 2, item.position.z);
      const width = mode === 'photos' ? Math.max(2, Math.min(6, span / 1200)) : Math.max(9, Math.min(23, span / 200));
      transform.scale.set(item.selected ? width * 2 : width, height, item.selected ? width * 2 : width);
      transform.updateMatrix(); columns.setMatrixAt(i, transform.matrix);
      columns.setColorAt(i, new THREE.Color(item.selected ? '#172d3b' : item.color));
    });
    columns.instanceMatrix.needsUpdate = true;
    s.content.add(columns); s.columns = columns; s.keys = items.map((item) => item.key); s.mode = mode;
    s.reset = () => {
      s.controls.target.copy(centre);
      s.camera.position.set(centre.x + span * .45, span * .85, centre.z + span * .95);
      s.controls.update(); s.render();
    };
    if (s.fitKey !== fitKey) { s.fitKey = fitKey; s.reset(); }
    s.render();
  }, [rows, points, metric, selected, selectedPhoto, fitKey, heightScale, heightMode, minimum, mode, spread]);
  return <div className="viz-three-wrap">
    <div ref={host} className="viz-three" />
    {error ? <div className="viz-render-error" role="alert">{error}</div> : <>
      <div className="viz-three-help"><strong>3D observation landscape</strong><span>Drag to orbit · Scroll to zoom · Right-drag to pan</span><span>{mode === 'photos' ? 'One column per tagged photo · Grid = 500 m' : 'Colour = percentage · Grid = 500 m'} · N marks north</span></div>
      <div className="viz-three-controls">
        <label>Render <select aria-label="3D data points" value={mode} onChange={(e) => { if (e.target.value === 'photos' || e.target.value === 'segments') setMode(e.target.value); }}><option value="photos">Every tagged photo</option><option value="segments">Segment summaries</option></select></label>
        <label>Column height <input aria-label="Column height" type="range" min="0" max="2" step="0.1" value={heightScale} onChange={(e) => setHeightScale(Number(e.target.value))} /><output>{heightScale.toFixed(1)}×</output></label>
        {mode === 'segments' && <><label>Height represents <select aria-label="Height represents" value={heightMode} onChange={(e) => { if (e.target.value === 'rate' || e.target.value === 'count') setHeightMode(e.target.value); }}><option value="rate">Percentage</option><option value="count">Photo count</option></select></label>
        <label>Minimum observations <select aria-label="3D minimum observations" value={minimum} onChange={(e) => setMinimum(Number(e.target.value))}>{[1, 5, 10, 20].map((n) => <option key={n} value={n}>{n} photos</option>)}</select></label></>}
        {mode === 'photos' ? <><label><input type="checkbox" checked={spread} onChange={(e) => setSpread(e.target.checked)} />Separate overlapping photos</label><small>{points.length.toLocaleString()} / {points.length.toLocaleString()} tagged photos rendered · no sampling or aggregation. Tall columns = recorded signal; low markers = other or unknown. {spread ? 'Coincident cameras spread up to 8 m for selection.' : 'Exact camera positions; some columns overlap.'}</small></> : <small>{rows.filter((row) => measure(row, metric).denominator >= minimum).length.toLocaleString()} of {rows.length.toLocaleString()} segments shown · {heightMode === 'rate' ? 'Tallest possible column = 100%' : `Tallest column = ${Math.max(0, ...rows.filter((row) => measure(row, metric).denominator >= minimum).map((row) => measure(row, metric).numerator))} photos`}. Heights are visual, not physical.</small>}
      </div>
      <div className="viz-legend"><strong>{mode === 'photos' ? 'Individual photo observations' : 'Segment percentages'}</strong><div>{(mode === 'photos' ? pointLegend(metric) : [['#a7b2b8', 'Unknown'], ['#25857b', 'Low'], ['#e2bd61', 'Moderate'], ['#b94749', 'High']]).map(([color, label]) => <span key={label}><i style={{ background: color }} />{label}</span>)}</div><small>Click a column to inspect its source evidence. Camera locations are not surveyed feature positions.</small></div>
      <button type="button" className="btn viz-reset" onClick={() => state.current?.reset()}>Reset camera</button>
    </>}
  </div>;
}
