import { useMemo, useState } from 'react';
import type { Atlas } from './context';
import { StackBar } from './charts';
import { fmt, percent, photoCodes } from './model';

type Sort = 'photos' | 'annotated' | 'focus' | 'absent' | 'length' | 'name';

export default function Streets({ atlas }: { atlas: Atlas }) {
  const { ds, sel, colorBy, focus } = atlas;
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<Sort>(focus ? 'focus' : 'photos');
  const [minimum, setMinimum] = useState(10);
  const [limit, setLimit] = useState(40);
  const focusIndex = focus ? colorBy.categories.findIndex((c) => c.key === focus) : -1;
  const footpath = atlas.dimMap.get('footpath')!;
  const streets = useMemo(() => {
    const size = colorBy.categories.length, S = ds.snapshot.streets.length;
    const photos = new Float64Array(S), annotated = new Float64Array(S), absent = new Float64Array(S), present = new Float64Array(S);
    const comp = new Float64Array(S * size), segments = new Map<number, Set<number>>();
    const fp = footpath.data().single!;
    for (let i = 0; i < ds.n; i++) {
      if (!sel.photo[i]) continue;
      const s = ds.p.street[i];
      photos[s]++;
      if (ds.p.annotation[i] === 1) annotated[s]++;
      if (fp[i] === 0) present[s]++; else if (fp[i] === 1) absent[s]++;
      for (const k of photoCodes(colorBy, i)) comp[s * size + k]++;
      let set = segments.get(s); if (!set) segments.set(s, set = new Set()); set.add(ds.p.segment[i]);
    }
    const out = [];
    for (let s = 0; s < S; s++) {
      if (!photos[s]) continue;
      const counts = comp.subarray(s * size, (s + 1) * size);
      let known = 0; colorBy.categories.forEach((c, k) => { if (!c.structural) known += counts[k]; });
      let length = 0; for (const seg of segments.get(s)!) length += ds.snapshot.segments.length_m[seg] ?? 0;
      out.push({ s, name: ds.snapshot.streets[s], photos: photos[s], annotated: annotated[s], counts, known, segments: segments.get(s)!.size, length,
        absent: absent[s] + present[s] ? absent[s] / (absent[s] + present[s]) : null, footpathKnown: absent[s] + present[s],
        share: focusIndex >= 0 && known ? counts[focusIndex] / known : null });
    }
    return out;
  }, [ds, sel, colorBy, focusIndex, footpath]);
  const q = search.trim().toLowerCase();
  const eligible = (r: (typeof streets)[number]) => sort === 'focus' ? r.known >= minimum : sort === 'absent' ? r.footpathKnown >= minimum : true;
  const rows = streets.filter((r) => (!q || r.name.toLowerCase().includes(q)) && eligible(r)).sort((a, b) =>
    sort === 'name' ? a.name.localeCompare(b.name) : sort === 'focus' ? (b.share ?? -1) - (a.share ?? -1) || b.known - a.known
      : sort === 'absent' ? (b.absent ?? -1) - (a.absent ?? -1) || b.footpathKnown - a.footpathKnown
        : sort === 'length' ? b.length - a.length : sort === 'annotated' ? b.annotated - a.annotated : b.photos - a.photos);
  const filtered = atlas.filters.cats.street ?? [];
  const inspect = (s: number, name: string) => {
    const list: number[] = [];
    for (let i = 0; i < ds.n; i++) if (sel.photo[i] && ds.p.street[i] === s) list.push(i);
    atlas.selectGroup({ title: name, subtitle: `${fmt(list.length)} photos on this street`, photos: list });
  };
  return <div className="viz-streets">
    <div className="viz-compare-controls">
      <input type="search" aria-label="Search streets" placeholder="Find a street…" value={search} onChange={(e) => { setSearch(e.target.value); setLimit(40); }} />
      <label>Sort <select aria-label="Sort streets" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
        <option value="photos">Most photos</option><option value="annotated">Most annotated</option><option value="length">Longest planned length</option>
        <option value="absent">Highest missing-footpath share</option><option value="focus" disabled={focusIndex < 0}>Highest share of highlighted value</option><option value="name">Name</option>
      </select></label>
      {(sort === 'focus' || sort === 'absent') && <label>Minimum evidence <select aria-label="Minimum street evidence" value={minimum} onChange={(e) => setMinimum(Number(e.target.value))}>{[1, 5, 10, 25, 50, 100].map((n) => <option key={n} value={n}>{n} photos</option>)}</select></label>}
      <label>Highlight <select aria-label="Streets highlight value" value={focus ?? ''} onChange={(e) => { atlas.setFocus(e.target.value || null); if (e.target.value) setSort('focus'); }}><option value="">None</option>{colorBy.categories.filter((c) => !c.structural).map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}</select></label>
    </div>
    <p className="viz-fine">{fmt(rows.length)} streets. Composition bars show <b>{colorBy.label}</b> for each street’s matching photos (change it with “Colour by”). Shares use photos with a known value and need at least the minimum evidence, so a tiny street cannot top the list with 1 of 1.</p>
    <div className="viz-table-scroll"><table className="viz-table viz-street-table">
      <thead><tr><th>Street</th><th>Photos</th><th>Annotated</th><th>Segments · length</th><th>No footpath</th><th>{colorBy.label}</th>{focusIndex >= 0 && <th>{colorBy.categories[focusIndex].label}</th>}</tr></thead>
      <tbody>{rows.slice(0, limit).map((r) => <tr key={r.s} className={filtered.includes(r.name) ? 'is-active' : ''}>
        <td><button type="button" className="viz-text-button" onClick={() => atlas.toggle('street', r.name, true)} aria-pressed={filtered.includes(r.name)}>{r.name}</button>
          <button type="button" className="viz-icon-button" aria-label={`Inspect photos on ${r.name}`} title="Inspect photos" onClick={() => inspect(r.s, r.name)}>⌕</button></td>
        <td>{fmt(r.photos)}</td><td>{fmt(r.annotated)} <small>{percent(r.annotated / r.photos)}</small></td>
        <td>{fmt(r.segments)} · {r.length >= 1000 ? `${(r.length / 1000).toFixed(1)} km` : `${Math.round(r.length)} m`}</td>
        <td>{r.absent === null ? '—' : <>{percent(r.absent)} <small>of {fmt(r.footpathKnown)}</small></>}</td>
        <td className="viz-street-comp"><StackBar label={`${r.name} ${colorBy.label}`} parts={colorBy.categories.map((c, k) => ({ key: c.key, label: c.label, value: r.counts[k], color: c.color }))} /></td>
        {focusIndex >= 0 && <td>{r.share === null ? '—' : <>{percent(r.share)} <small>of {fmt(r.known)}</small></>}</td>}
      </tr>)}</tbody>
    </table></div>
    {!rows.length && <p className="viz-empty">No streets match. Lower the minimum evidence or clear the search.</p>}
    {limit < rows.length && <button type="button" className="btn" onClick={() => setLimit(limit + 40)}>Show 40 more streets</button>}
  </div>;
}
