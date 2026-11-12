import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { fmt, percent, type Category, type Dimension, type Tally, type Unit } from './model';

export const UNIT_WORD: Record<Unit, [string, string]> = {
  photos: ['photo', 'photos'], panoramas: ['panorama', 'panoramas'], segments: ['segment', 'segments'], features: ['feature', 'features'],
};
export const unitWord = (unit: Unit, count: number) => UNIT_WORD[unit][count === 1 ? 0 : 1];
export function unitFor(dim: Dimension, unit: Unit): string {
  if (unit === 'features' && dim.level === 'feature') return dim.noun;
  return unitWord(unit === 'features' && dim.level !== 'feature' ? 'photos' : unit, 2);
}

export function Swatch({ color, hollow = false }: { color: string; hollow?: boolean }) {
  return <i className={`viz-swatch ${hollow ? 'is-hollow' : ''}`} style={{ background: hollow ? 'transparent' : color, borderColor: color }} aria-hidden="true" />;
}

/** Horizontal ranked bars for one dimension. Known values first, structural states (unknown / not recorded / missing) below. */
export function BarList({ dim, tally, baseline, selected, onToggle, limit = 12, compact = false, shareOf = 'known' }: {
  dim: Dimension; tally: Tally; baseline?: Tally; selected?: string[]; onToggle?: (key: string, only: boolean) => void;
  limit?: number; compact?: boolean; shareOf?: 'known' | 'total';
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = dim.categories.map((c, k) => ({ c, k, value: tally.counts[k], base: baseline?.counts[k] ?? 0 }));
  const known = rows.filter((r) => !r.c.structural && (r.value > 0 || (selected ?? []).includes(r.c.key)));
  const structural = rows.filter((r) => r.c.structural && (r.value > 0 || (selected ?? []).includes(r.c.key)));
  if (!dim.ordinal) known.sort((a, b) => b.value - a.value || b.base - a.base);
  const knownSum = known.reduce((s, r) => s + r.value, 0);
  const denominator = shareOf === 'known' ? knownSum : tally.total;
  const max = Math.max(1, ...known.map((r) => Math.max(r.value, r.base)), ...structural.map((r) => Math.max(r.value, r.base)));
  const hidden = dim.categories.length - known.length - structural.length;
  const visible = expanded ? known : known.slice(0, limit);
  const word = unitFor(dim, tally.unit);
  const row = ({ c, value, base }: (typeof rows)[number]) => {
    const active = (selected ?? []).includes(c.key);
    const content = <>
      <span className="viz-bar-label"><Swatch color={c.color} hollow={c.structural} />{c.label}</span>
      <span className="viz-bar-track" aria-hidden="true">
        {baseline && <span className="viz-bar-ghost" style={{ width: `${base / max * 100}%` }} />}
        <span className={`viz-bar-fill ${c.structural ? 'is-structural' : ''}`} style={{ width: `${value / max * 100}%`, background: c.color }} />
      </span>
      <span className="viz-bar-value"><b>{fmt(value)}</b>{!c.structural && denominator > 0 && <small>{percent(value / denominator, value / denominator < .01 ? 1 : 0)}</small>}</span>
    </>;
    const title = `${c.label}: ${fmt(value)} ${word}${!c.structural && denominator ? ` (${percent(value / denominator, 1)} of ${fmt(denominator)} with a known value)` : ''}${baseline ? ` · ${fmt(base)} in the whole dataset` : ''}`;
    return onToggle
      ? <button type="button" key={c.key} className={`viz-bar-row ${active ? 'is-active' : ''}`} aria-pressed={active} title={title}
        onClick={(e) => onToggle(c.key, !(e.shiftKey || e.metaKey || e.ctrlKey))}>{content}</button>
      : <div key={c.key} className="viz-bar-row" title={title}>{content}</div>;
  };
  return <div className={`viz-barlist ${compact ? 'is-compact' : ''}`}>
    {visible.map(row)}
    {known.length > limit && <button type="button" className="viz-text-button viz-more" onClick={() => setExpanded(!expanded)}>{expanded ? 'Show fewer' : `Show all ${known.length} values`}</button>}
    {structural.length > 0 && <div className="viz-bar-divider"><span>Unknown, not recorded or missing</span></div>}
    {structural.map(row)}
    {!known.length && !structural.length && <p className="viz-fine">No {word} match the current filters.</p>}
    {hidden > 0 && !compact && <p className="viz-fine">{hidden} value{hidden === 1 ? '' : 's'} with no matching {word} hidden.</p>}
  </div>;
}

/** A single 100% bar split into categories. */
export function StackBar({ parts, height = 14, label }: { parts: { key: string; label: string; value: number; color: string }[]; height?: number; label: string }) {
  const total = parts.reduce((s, p) => s + p.value, 0);
  return <div className="viz-stack" style={{ height }} role="img" aria-label={`${label}: ${parts.filter((p) => p.value).map((p) => `${p.label} ${fmt(p.value)}`).join(', ')}`}>
    {parts.filter((p) => p.value > 0).map((p) => <span key={p.key} style={{ flexGrow: p.value, background: p.color }} title={`${p.label}: ${fmt(p.value)} (${percent(total ? p.value / total : null, 1)})`} />)}
  </div>;
}

export type Series = { key: string; label: string; color: string };
/**
 * Vertical columns over an ordered numeric domain, optionally stacked by series.
 * Drag across columns to select a range.
 */
export function Columns({ domain, values, series, label, tickLabel, tooltip, mode = 'count', onRange, range, height = 220 }: {
  domain: number[]; values: Map<number, Float64Array>; series: Series[]; label: string;
  tickLabel: (v: number) => string; tooltip: (v: number) => string; mode?: 'count' | 'share';
  onRange?: (range: [number, number] | null) => void; range?: [number, number] | null; height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [drag, setDrag] = useState<[number, number] | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(900);
  useEffect(() => {
    const el = svg.current?.parentElement;
    if (!el) return;
    const observer = new ResizeObserver(() => setWidth(Math.max(280, Math.round(el.clientWidth))));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const top = 10, bottom = 26, left = 46, right = 8, plotH = height - top - bottom;
  const totals = useMemo(() => domain.map((d) => (values.get(d) ?? new Float64Array(series.length)).reduce((a, b) => a + b, 0)), [domain, values, series.length]);
  const max = mode === 'share' ? 1 : Math.max(1, ...totals);
  const step = (width - left - right) / Math.max(1, domain.length);
  const barW = Math.max(1, step - (step > 6 ? 2 : step > 3 ? 1 : 0));
  const labels = useMemo(() => {
    const raw = domain.map(tickLabel), shown = raw.filter(Boolean).length, every = Math.max(1, Math.ceil(shown / Math.max(3, Math.floor(width / 75))));
    let seen = 0;
    return raw.map((text) => text && seen++ % every === 0 ? text : '');
  }, [domain, tickLabel, width]);
  const indexAt = (clientX: number) => {
    const rect = svg.current?.getBoundingClientRect();
    if (!rect) return 0;
    const x = (clientX - rect.left) / rect.width * width;
    return Math.max(0, Math.min(domain.length - 1, Math.floor((x - left) / step)));
  };
  const sel = drag ?? (range ? [domain.indexOf(range[0]), domain.indexOf(range[1])] as [number, number] : null);
  const ticks = [0, .25, .5, .75, 1];
  const hoverIndex = hover;
  return <div className="viz-columns">
    <svg ref={svg} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} preserveAspectRatio="none"
      onPointerDown={onRange ? (e) => { const i = indexAt(e.clientX); setDrag([i, i]); (e.target as Element).setPointerCapture?.(e.pointerId); } : undefined}
      onPointerMove={(e) => { const i = indexAt(e.clientX); setHover(i); if (drag) setDrag([drag[0], i]); }}
      onPointerLeave={() => setHover(null)}
      onPointerUp={onRange ? () => { if (drag) { const [a, b] = [Math.min(...drag), Math.max(...drag)]; onRange([domain[a], domain[b]]); } setDrag(null); } : undefined}>
      {ticks.map((t) => <g key={t}><line x1={left} x2={width - right} y1={top + plotH - t * plotH} y2={top + plotH - t * plotH} className="viz-grid" />
        <text x={left - 6} y={top + plotH - t * plotH + 4} textAnchor="end" className="viz-axis">{mode === 'share' ? `${t * 100}%` : fmt(Math.round(t * max))}</text></g>)}
      {sel && sel[0] >= 0 && sel[1] >= 0 && <rect x={left + Math.min(...sel) * step} y={top} width={(Math.abs(sel[1] - sel[0]) + 1) * step} height={plotH} className="viz-brush" />}
      {domain.map((d, i) => {
        const row = values.get(d), total = totals[i];
        let y = top + plotH;
        return <g key={d} opacity={hoverIndex === null || hoverIndex === i ? 1 : .55}>
          {row && series.map((s, k) => {
            const v = row[k];
            if (!v) return null;
            const h = (mode === 'share' ? v / Math.max(1, total) : v / max) * plotH;
            y -= h;
            return <rect key={s.key} x={left + i * step} y={y} width={barW} height={Math.max(0, h - (h > 3 ? .6 : 0))} fill={s.color} />;
          })}
          {labels[i] && <text x={left + i * step + barW / 2} y={height - 8} textAnchor="middle" className="viz-axis">{labels[i]}</text>}
        </g>;
      })}
    </svg>
    <div className="viz-columns-readout" aria-live="polite">{hoverIndex !== null && domain[hoverIndex] !== undefined ? <>
      <strong>{tooltip(domain[hoverIndex])}</strong> · {fmt(totals[hoverIndex])}
      {series.length > 1 && values.get(domain[hoverIndex]) && <span>{series.map((s, k) => ({ s, v: values.get(domain[hoverIndex])![k] })).filter((x) => x.v).sort((a, b) => b.v - a.v).slice(0, 5).map(({ s, v }) => <span key={s.key}><Swatch color={s.color} />{s.label} {fmt(v)}</span>)}</span>}
    </> : <span className="viz-fine">{onRange ? 'Hover for values · drag across columns to filter a range' : 'Hover for values'}</span>}</div>
  </div>;
}

export function Card({ title, subtitle, children, actions, className = '' }: { title: string; subtitle?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string }) {
  return <section className={`viz-card ${className}`}><header><div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div>{actions}</header>{children}</section>;
}

export function Legend({ categories, counts, selected, onToggle, title, note }: {
  categories: Category[]; counts?: Float64Array; selected?: string[]; onToggle?: (key: string, only: boolean) => void; title: string; note?: ReactNode;
}) {
  const rows = categories.map((c, k) => ({ c, v: counts?.[k] ?? 0 })).filter((r) => !counts || r.v > 0 || selected?.includes(r.c.key));
  return <div className="viz-legend-panel"><strong>{title}</strong>
    <div className="viz-legend-rows">{rows.map(({ c, v }) => {
      const active = selected?.includes(c.key);
      const body = <><Swatch color={c.color} hollow={c.structural} /><span>{c.label}</span>{counts && <b>{fmt(v)}</b>}</>;
      return onToggle ? <button type="button" key={c.key} aria-pressed={!!active} className={active ? 'is-active' : ''} title="Click to show only this value; shift-click to add" onClick={(e) => onToggle(c.key, !(e.shiftKey || e.metaKey || e.ctrlKey))}>{body}</button> : <div key={c.key}>{body}</div>;
    })}</div>{note && <small>{note}</small>}</div>;
}

export function DimensionSelect({ dims, value, onChange, label, filter, placeholder }: { dims: Dimension[]; value: string; onChange: (id: string) => void; label: string; filter?: (d: Dimension) => boolean; placeholder?: string }) {
  const groups = new Map<string, Dimension[]>();
  for (const d of dims) if (!filter || filter(d)) groups.set(d.group, [...(groups.get(d.group) ?? []), d]);
  return <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
    {placeholder !== undefined && <option value="">{placeholder}</option>}
    {[...groups].map(([group, items]) => <optgroup key={group} label={group}>{items.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}</optgroup>)}
  </select>;
}
