import { useMemo, useState } from 'react';
import { addCounts, colour, emptyCounts, measure, METRICS, metricInfo, percent, type Counts, type Metric, type Segment } from './data';

export default function Insights({ rows, metric, onExplore }: {
  rows: Segment[]; metric: Metric; onExplore: (street: string, metric: Metric) => void;
}) {
  const [minimum, setMinimum] = useState(5);
  const [search, setSearch] = useState('');
  const [limit, setLimit] = useState(30);
  const streets = useMemo(() => {
    const groups = new Map<string, Counts>();
    for (const row of rows) {
      const group = groups.get(row.street) ?? emptyCounts();
      addCounts(group, row); groups.set(row.street, group);
    }
    return [...groups].map(([street, counts]) => ({ street, counts, ...measure(counts, metric) }));
  }, [rows, metric]);
  const matching = streets.filter((row) => row.street.toLowerCase().includes(search.toLowerCase()));
  const ranked = matching.filter((row) => row.denominator >= minimum)
    .sort((a, b) => b.numerator - a.numerator || b.denominator - a.denominator || a.street.localeCompare(b.street));
  const points = ranked.slice(0, 200);
  const maxEvidence = Math.max(1, ...points.map((row) => row.denominator));
  const matchingSegments = rows.filter((row) => row.street.toLowerCase().includes(search.toLowerCase()));
  const bins = [0, 0, 0, 0, 0];
  let excluded = 0;
  for (const row of matchingSegments) {
    const value = measure(row, metric);
    if (value.denominator < minimum || value.rate === null) { excluded++; continue; }
    const index = value.rate === 0 ? 0 : value.rate < .25 ? 1 : value.rate < .5 ? 2 : value.rate < .75 ? 3 : 4;
    bins[index]++;
  }
  const maxBin = Math.max(1, ...bins);
  const binLabels = ['0%', '>0–<25%', '25–<50%', '50–<75%', '75–100%'];
  const binColours = metric === 'coverage' ? ['#a7b2b8', '#a0c8c0', '#74b3ab', '#399a98', '#126e68'] : ['#4f969b', '#e2bd61', '#db884f', '#c9694c', '#b94749'];
  return <div className="viz-insights">
    <div className="viz-insights-head"><div><h2>Patterns & evidence</h2><p>{metricInfo(metric).name} · {ranked.length.toLocaleString()} streets meet the evidence threshold.</p></div>
      <label>Minimum observations <select aria-label="Insights minimum observations" value={minimum} onChange={(e) => setMinimum(Number(e.target.value))}>{[1, 5, 10, 20, 50].map((n) => <option key={n} value={n}>{n} photos</option>)}</select></label>
      <input aria-label="Search insights streets" placeholder="Search streets…" value={search} onChange={(e) => setSearch(e.target.value)} />
    </div>
    <div className="viz-insight-charts">
      <section className="viz-insight-card"><h3>Rate versus evidence volume</h3><p>Each dot is a street. Further right means more observations; higher means a larger percentage. Select a dot to inspect its map.</p>
        <svg viewBox="0 0 620 330" role="group" aria-label="Street observation rate versus evidence volume">
          {[0, .25, .5, .75, 1].map((tick) => <g key={tick}><line x1="55" x2="590" y1={280 - tick * 250} y2={280 - tick * 250} stroke="#dce4df" /><text x="45" y={284 - tick * 250} textAnchor="end">{Math.round(tick * 100)}%</text></g>)}
          {[0, .25, .5, .75, 1].map((tick) => <text key={tick} x={55 + tick * 535} y="300" textAnchor="middle">{Math.round(tick * maxEvidence)}</text>)}
          <text x="320" y="324" textAnchor="middle">Photos in the selected measure’s denominator</text>
          {points.map((row) => <circle key={row.street} cx={55 + row.denominator / maxEvidence * 535} cy={280 - (row.rate ?? 0) * 250} r={5} fill={colour(row.counts, metric)} stroke="white" strokeWidth="1" fillOpacity=".8" tabIndex={0} role="button" aria-label={`${row.street}: ${row.numerator} of ${row.denominator} photos, ${percent(row.rate)}`} onClick={() => onExplore(row.street, metric)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onExplore(row.street, metric); } }}><title>{row.street}: {row.numerator}/{row.denominator} ({percent(row.rate)}) · {row.counts.tagged}/{row.counts.planned} tagged</title></circle>)}
        </svg>
        <small>{points.length} of {ranked.length} qualifying streets shown, ordered by signal-photo count. Overlapping dots can share the same values.</small>
      </section>
      <section className="viz-insight-card"><h3>How segment rates are distributed</h3><p>Counts of street segments within each percentage range. Each segment counts once, regardless of its photo count.</p>
        <div className="viz-distribution" aria-label="Segment rate distribution">{bins.map((count, i) => <div key={binLabels[i]}><strong>{count.toLocaleString()}</strong><div className="viz-distribution-track"><span style={{ height: `${count / maxBin * 100}%`, background: binColours[i] }} /></div><span>{binLabels[i]}</span></div>)}</div>
        <small>{matchingSegments.length - excluded} qualifying segments · {excluded} excluded for insufficient observations. No observation is different from 0%.</small>
      </section>
    </div>
    <section className="viz-insight-card"><h3>Street profiles across all measures</h3><p>Rows qualify using the selected layer. Each cell applies the minimum to its own denominator; grey cells have too little evidence. Select a cell to explore that measure on the map. Coverage measures completion, not accessibility.</p>
      <div className="viz-heatmap-scroll"><table className="viz-heatmap"><thead><tr><th>Street</th>{METRICS.map((m) => <th key={m.key}>{m.key === 'coverage' ? 'Tagging coverage' : m.short}</th>)}</tr></thead><tbody>{ranked.slice(0, limit).map((row) => <tr key={row.street}><th>{row.street}</th>{METRICS.map((m) => {
        const value = measure(row.counts, m.key); const enough = value.denominator >= minimum;
        return <td key={m.key}><button disabled={!enough} onClick={() => onExplore(row.street, m.key)} style={{ borderLeftColor: enough ? colour(row.counts, m.key) : '#cbd2ce' }} aria-label={`${row.street}, ${m.name}: ${value.numerator} of ${value.denominator}`}><strong>{enough ? percent(value.rate) : 'Too few'}</strong><small>{value.numerator} / {value.denominator}</small></button></td>;
      })}</tr>)}</tbody></table></div>
      {!ranked.length && <p className="viz-empty">No streets meet these filters. Lower the minimum observations or clear the street search.</p>}
      <div className="viz-profile-footer"><small>Showing {Math.min(limit, ranked.length)} of {ranked.length} streets, ranked by {metricInfo(metric).short.toLowerCase()} photo count. Measures can overlap; their counts must not be added together.</small>{limit < ranked.length && <button className="btn" onClick={() => setLimit((n) => n + 30)}>Show 30 more streets</button>}</div>
    </section>
  </div>;
}
