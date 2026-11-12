import { useMemo, useState } from 'react';
import type { Atlas } from './context';
import { DimensionSelect, Swatch, unitWord } from './charts';
import { crossTab, fmt, heat, matrixCsv, percent } from './model';
import { download } from './download';

const PRESETS: { label: string; rows: string; cols: string; unit?: 'features' | 'photos' | 'panoramas' }[] = [
  { label: 'Surface × footpath type', rows: 'tag:footpath_section:surface', cols: 'tag:footpath_section:path_type', unit: 'features' },
  { label: 'Surface × imagery year', rows: 'tag:footpath_section:surface', cols: 'imagery_year', unit: 'features' },
  { label: 'Surface × road class', rows: 'tag:footpath_section:surface', cols: 'highway', unit: 'features' },
  { label: 'Hazard type × route relation', rows: 'tag:hazard:type', cols: 'tag:hazard:route_relation', unit: 'features' },
  { label: 'Hazard type × persistence', rows: 'tag:hazard:type', cols: 'tag:hazard:persistence', unit: 'features' },
  { label: 'Hazard type × passage', rows: 'tag:hazard:type', cols: 'tag:hazard:clear_passage', unit: 'features' },
  { label: 'Footpath presence × OSM sidewalk', rows: 'footpath', cols: 'osm_sidewalk', unit: 'photos' },
  { label: 'Continuity × interruption', rows: 'tag:footpath_section:continuity', cols: 'tag:footpath_section:interruption', unit: 'features' },
  { label: 'Driveway treatment × surface', rows: 'tag:driveway:footpath_treatment', cols: 'tag:driveway:surface', unit: 'features' },
  { label: 'Imagery age × review notes', rows: 'imagery_age', cols: 'review_notes', unit: 'photos' },
  { label: 'Confidence × footpath presence', rows: 'min_confidence', cols: 'footpath', unit: 'photos' },
  { label: 'Ramp tactile × alignment', rows: 'tag:kerb_ramp:tactile_indicators', cols: 'tag:kerb_ramp:alignment', unit: 'features' },
];
type Norm = 'count' | 'row' | 'col';

export default function Compare({ atlas }: { atlas: Atlas }) {
  const { ds, dims, dimMap, sel } = atlas;
  const [rowsId, setRows] = useState(PRESETS[0].rows);
  const [colsId, setCols] = useState(PRESETS[0].cols);
  const [unit, setUnit] = useState<'features' | 'photos' | 'panoramas' | 'segments'>('features');
  const [norm, setNorm] = useState<Norm>('row');
  const [structural, setStructural] = useState(false);
  const rows = dimMap.get(rowsId)!, cols = dimMap.get(colsId)!;
  const m = useMemo(() => crossTab(ds, rows, cols, sel, unit), [ds, rows, cols, sel, unit]);
  const R = rows.categories.length, C = cols.categories.length;
  const rowIdx = rows.categories.map((_, k) => k).filter((k) => m.rowTotals[k] > 0 && (structural || !rows.categories[k].structural));
  const colIdx = cols.categories.map((_, k) => k).filter((k) => m.colTotals[k] > 0 && (structural || !cols.categories[k].structural));
  if (!rows.ordinal) rowIdx.sort((a, b) => m.rowTotals[b] - m.rowTotals[a]);
  const rowSum = (r: number) => colIdx.reduce((s, c) => s + m.counts[r * C + c], 0);
  const colSum = (c: number) => rowIdx.reduce((s, r) => s + m.counts[r * C + c], 0);
  const value = (r: number, c: number) => {
    const v = m.counts[r * C + c];
    return norm === 'count' ? v : norm === 'row' ? v / Math.max(1, rowSum(r)) : v / Math.max(1, colSum(c));
  };
  let max = 0;
  for (const r of rowIdx) for (const c of colIdx) max = Math.max(max, value(r, c));
  const hidden = (R - rowIdx.length) + (C - colIdx.length);
  const word = unitWord(m.unit, 2);
  const apply = (r: number, c: number) => {
    let f = atlas.filters;
    f = { ...f, cats: { ...f.cats, [rows.id]: [rows.categories[r].key], [cols.id]: [cols.categories[c].key] } };
    atlas.setFilters(f);
  };
  return <div className="viz-compare">
    <div className="viz-presets" role="group" aria-label="Comparison presets">{PRESETS.map((p) => <button key={p.label} type="button" aria-pressed={p.rows === rowsId && p.cols === colsId}
      onClick={() => { setRows(p.rows); setCols(p.cols); if (p.unit) setUnit(p.unit); }}>{p.label}</button>)}</div>
    <div className="viz-compare-controls">
      <label>Rows <DimensionSelect dims={dims} value={rowsId} onChange={setRows} label="Rows attribute" /></label>
      <button type="button" className="btn" aria-label="Swap rows and columns" onClick={() => { setRows(colsId); setCols(rowsId); }}>⇄</button>
      <label>Columns <DimensionSelect dims={dims} value={colsId} onChange={setCols} label="Columns attribute" /></label>
      <label>Count <select aria-label="Comparison unit" value={unit} onChange={(e) => setUnit(e.target.value as typeof unit)}><option value="features">Features</option><option value="photos">Photos</option><option value="panoramas">Unique panoramas</option><option value="segments">Segments</option></select></label>
      <div className="seg" role="group" aria-label="Cell values">{([['count', 'Counts'], ['row', 'Row %'], ['col', 'Column %']] as [Norm, string][]).map(([k, l]) => <button key={k} type="button" aria-pressed={norm === k} onClick={() => setNorm(k)}>{l}</button>)}</div>
      <label className="viz-check"><input type="checkbox" checked={structural} onChange={(e) => setStructural(e.target.checked)} />Show unknown / not recorded</label>
      <button type="button" className="btn" onClick={() => download(`maply-${rows.id}-by-${cols.id}.csv`.replaceAll(':', '_'), matrixCsv(m), 'text/csv')}>Export table</button>
    </div>
    <p className="viz-fine"><b>{fmt(m.total)}</b> {word} counted{m.unit !== unit ? ` (requested ${unit})` : ''}. {m.note} {norm === 'row' ? 'Each row sums to 100% across the visible columns.' : norm === 'col' ? 'Each column sums to 100% across the visible rows.' : ''} {hidden > 0 && `${hidden} empty or ${structural ? '' : 'unknown/not-recorded '}rows/columns hidden.`} Click a cell to filter to it.</p>
    {!rowIdx.length || !colIdx.length ? <div className="viz-empty"><h2>Nothing to cross-tabulate</h2><p>No {word} have known values for both attributes under the current filters. Try showing unknown values or clearing filters.</p></div> :
      <div className="viz-matrix-scroll"><table className="viz-matrix">
        <thead><tr><th scope="col" className="viz-matrix-corner">{rows.label} <span>↓</span> · {cols.label} <span>→</span></th>{colIdx.map((c) => <th key={c} scope="col"><span><Swatch color={cols.categories[c].color} hollow={cols.categories[c].structural} />{cols.categories[c].label}</span></th>)}<th scope="col">Total</th></tr></thead>
        <tbody>{rowIdx.map((r) => <tr key={r}><th scope="row"><Swatch color={rows.categories[r].color} hollow={rows.categories[r].structural} />{rows.categories[r].label}</th>
          {colIdx.map((c) => {
            const v = value(r, c), raw = m.counts[r * C + c], t = max ? v / max : 0;
            return <td key={c}><button type="button" style={{ background: raw ? heat(.08 + t * .92) : undefined, color: t > .55 ? '#fff' : undefined }} onClick={() => apply(r, c)}
              title={`${rows.categories[r].label} × ${cols.categories[c].label}: ${fmt(raw)} ${word}${norm !== 'count' ? ` (${percent(v, 1)} of ${norm === 'row' ? 'row' : 'column'})` : ''}`}
              aria-label={`${rows.categories[r].label} and ${cols.categories[c].label}: ${fmt(raw)} ${word}`}>
              {raw ? norm === 'count' ? fmt(raw) : percent(v, v < .1 ? 1 : 0) : '·'}</button></td>;
          })}
          <td className="viz-matrix-total">{fmt(rowSum(r))}</td></tr>)}</tbody>
        <tfoot><tr><th scope="row">Total</th>{colIdx.map((c) => <td key={c} className="viz-matrix-total">{fmt(colSum(c))}</td>)}<td className="viz-matrix-total">{fmt(rowIdx.reduce((s, r) => s + rowSum(r), 0))}</td></tr></tfoot>
      </table></div>}
    <div className="viz-compare-notes"><p><strong>{rows.label}.</strong> {rows.description}</p><p><strong>{cols.label}.</strong> {cols.description}</p></div>
  </div>;
}
