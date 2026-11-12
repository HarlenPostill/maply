import { useMemo } from 'react';
import type { Atlas } from './context';
import { BarList, Card, StackBar, Swatch } from './charts';
import { attributeCoverage, fmt, KIND_PLURAL, METRIC_INFO, NOT_RECORDED, percent, tally, UNKNOWN, words } from './model';

const PARTS = [
  { key: 'clear', label: 'Known, clearly visible', color: '#2a78d6' },
  { key: 'partial', label: 'Known, partial/blurred', color: '#86b6ef' },
  { key: 'unknown', label: 'Emitted as unknown', color: UNKNOWN },
  { key: 'missing', label: 'Not recorded', color: NOT_RECORDED },
];

export default function Coverage({ atlas }: { atlas: Atlas }) {
  const { ds, sel, dimMap } = atlas;
  const rows = useMemo(() => attributeCoverage(ds, sel), [ds, sel]);
  const annotation = dimMap.get('annotation')!;
  const pipeline = useMemo(() => tally(ds, annotation, sel, 'photos'), [ds, annotation, sel]);
  const metrics = ds.vocab.metrics.map((m) => ({ m, dim: dimMap.get(`metric:${m}`)! })).filter((x) => x.dim);
  const explore = (id: string) => { if (dimMap.has(id)) { atlas.setColorBy(id); atlas.goTo('map'); } };
  return <div className="viz-coverage">
    <div className="viz-card-grid is-wide">
      <Card title="Photo coverage" subtitle="Which planned photos have a current annotation, and why the rest do not. Gaps are listed, never hidden.">
        <BarList dim={annotation} tally={pipeline} selected={atlas.filters.cats.annotation} onToggle={(k, only) => atlas.toggle('annotation', k, only)} shareOf="total" />
      </Card>
      <Card title="How to read missing values" subtitle="Four different things that are easy to conflate.">
        <dl className="viz-definitions">
          <dt><Swatch color="#2a78d6" />Known value</dt><dd>The model emitted a value with confidence. Partial or blurred evidence is allowed and shown separately.</dd>
          <dt><Swatch color={UNKNOWN} hollow />Unknown</dt><dd>The tag was emitted but the model could not determine it (often occluded or out of frame).</dd>
          <dt><Swatch color={NOT_RECORDED} hollow />Not recorded</dt><dd>The tag was never emitted for that feature. Most optional detail tags (condition, texture, slopes) were not assessed on most footpath sections. This is <em>not</em> “no defect”.</dd>
          <dt><Swatch color="#e2e6e8" hollow />Missing photo / feature</dt><dd>No current annotation, or no feature of that kind in the photo. Absence of a hazard record does not establish absence of hazards.</dd>
        </dl>
      </Card>
    </div>
    <Card title="Attribute coverage by feature" subtitle={<>Every tag in the schema, counted per annotated feature under the current filters. Select a tag to map it.</>}>
      <div className="viz-inline-legend is-static">{PARTS.map((p) => <span key={p.key}><Swatch color={p.color} hollow={p.key === 'missing' || p.key === 'unknown'} />{p.label}</span>)}</div>
      {ds.vocab.kinds.map((kind) => {
        const items = rows.filter((r) => r.kind === kind);
        const total = items[0]?.features ?? 0;
        return <div key={kind} className="viz-coverage-kind">
          <h4>{words(kind)} <span>{fmt(total)} {KIND_PLURAL[kind]}</span></h4>
          {!total ? <p className="viz-fine">No {KIND_PLURAL[kind]} in the current selection.</p> : <table className="viz-table viz-coverage-table">
            <thead><tr><th>Tag</th><th>Coverage</th><th>Known</th><th>Unknown</th><th>Not recorded</th><th>Mean confidence</th></tr></thead>
            <tbody>{items.map((r) => <tr key={r.tag}>
              <td><button type="button" className="viz-text-button" onClick={() => explore(`tag:${kind}:${r.tag}`)}>{words(r.tag)}</button></td>
              <td><StackBar label={`${words(kind)} ${words(r.tag)} coverage`} parts={[
                { key: 'clear', label: 'Known, clearly visible', value: r.known - r.partial, color: PARTS[0].color },
                { key: 'partial', label: 'Known, partial/blurred', value: r.partial, color: PARTS[1].color },
                { key: 'unknown', label: 'Unknown', value: r.unknown, color: PARTS[2].color },
                { key: 'missing', label: 'Not recorded', value: r.notRecorded, color: PARTS[3].color },
              ]} /></td>
              <td><b>{fmt(r.known)}</b> <small>{percent(r.known / total, r.known / total < .1 ? 1 : 0)}</small></td>
              <td>{fmt(r.unknown)}</td><td>{fmt(r.notRecorded)}</td>
              <td>{r.confN ? `${Math.round(r.confSum / r.confN)}%` : '—'}</td>
            </tr>)}</tbody>
          </table>}
        </div>;
      })}
      <p className="viz-fine">Model confidence is self-reported and uncalibrated; it is not a probability of accessibility.</p>
    </Card>
    <Card title="Derived concern indicator rules" subtitle="Each indicator is a fixed rule over existing tags, evaluated once per photo. There are no severity grades in the schema, and none are invented here.">
      <table className="viz-table viz-metric-table">
        <thead><tr><th>Indicator</th><th>Rule</th><th>Concern</th><th>No concern recorded</th><th>Not assessed</th><th>Share of assessed</th></tr></thead>
        <tbody>{metrics.map(({ m, dim }) => {
          const t = tally(ds, dim, sel, 'photos'), assessed = t.counts[0] + t.counts[1];
          return <tr key={m}><td><button type="button" className="viz-text-button" onClick={() => explore(dim.id)}>{METRIC_INFO[m]?.name ?? words(m)}</button></td>
            <td className="viz-rule">{METRIC_INFO[m]?.rule}</td><td><b>{fmt(t.counts[0])}</b></td><td>{fmt(t.counts[1])}</td><td>{fmt(t.counts[2])}</td>
            <td>{assessed ? percent(t.counts[0] / assessed, 1) : '—'} <small>of {fmt(assessed)}</small></td></tr>;
        })}</tbody>
      </table>
      <p className="viz-fine">Photos are the counting unit. Overlapping photos of the same panorama can repeat a concern; switch the Compare view to unique panoramas to remove duplicates. Features explicitly beside the route never contribute.</p>
    </Card>
  </div>;
}
