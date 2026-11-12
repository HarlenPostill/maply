import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Atlas } from './context';
import { Swatch } from './charts';
import { fmt, monthLabel, photoCodes } from './model';

type Order = 'survey' | 'shuffled' | 'notes' | 'oldest' | 'newest' | 'confidence';
const PAGE = 36;
const hash = (text: string) => { let h = 2166136261; for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619); return h >>> 0; };

export default function Gallery({ atlas, photos: subset }: { atlas: Atlas; photos?: number[] }) {
  const { ds, sel, colorBy } = atlas;
  const [order, setOrder] = useState<Order>('shuffled');
  const [page, setPage] = useState(0);
  const [onlyImages, setOnlyImages] = useState(true);
  const [thumbs, setThumbs] = useState<Record<string, string>>({});
  const list = useMemo(() => {
    const out: number[] = [];
    if (subset) out.push(...subset);
    else for (let i = 0; i < ds.n; i++) if (sel.photo[i]) out.push(i);
    const items = onlyImages ? out.filter((i) => ds.p.annotation[i] !== 0 || ['captured', 'accepted', 'needs_check'].includes(ds.vocab.status[ds.p.status[i]] ?? '')) : out;
    const m = ds.p.imagery_month;
    const key: Record<Order, (i: number) => number> = {
      survey: (i) => i, shuffled: (i) => hash(ds.p.id[i]), notes: (i) => -ds.p.review_reasons[i],
      oldest: (i) => m[i] < 0 ? 1e9 : m[i], newest: (i) => m[i] < 0 ? 1e9 : -m[i], confidence: (i) => ds.minConf[i] < 0 ? 1e9 : ds.minConf[i],
    };
    return items.sort((a, b) => key[order](a) - key[order](b) || a - b);
  }, [ds, sel, subset, order, onlyImages]);
  const pages = Math.max(1, Math.ceil(list.length / PAGE)), current = Math.min(page, pages - 1);
  const visible = list.slice(current * PAGE, current * PAGE + PAGE);
  const ids = visible.map((i) => ds.p.id[i]).filter((id) => !(id in thumbs));
  useEffect(() => {
    if (!ids.length) return;
    let disposed = false;
    api.get<Record<string, string>>(`/api/jobs/${ds.snapshot.job_id}/thumbs?ids=${ids.join(',')}`)
      .then((found) => { if (!disposed) setThumbs((t) => ({ ...t, ...Object.fromEntries(ids.map((id) => [id, found[id] ?? ''])) })); })
      .catch(() => { if (!disposed) setThumbs((t) => ({ ...t, ...Object.fromEntries(ids.map((id) => [id, ''])) })); });
    return () => { disposed = true; };
  }, [ids.join(','), ds.snapshot.job_id]);
  useEffect(() => setPage(0), [sel, order, subset]);
  return <div className="viz-gallery">
    <div className="viz-compare-controls">
      <label>Order <select aria-label="Photo order" value={order} onChange={(e) => setOrder(e.target.value as Order)}>
        <option value="shuffled">Stable shuffle (unbiased sample)</option><option value="survey">Survey order</option><option value="notes">Most review notes</option>
        <option value="confidence">Lowest model confidence</option><option value="oldest">Oldest imagery</option><option value="newest">Newest imagery</option>
      </select></label>
      <label className="viz-check"><input type="checkbox" checked={onlyImages} onChange={(e) => setOnlyImages(e.target.checked)} />Only photos with a saved image</label>
      <span className="viz-fine">{fmt(list.length)} photos · page {current + 1} of {fmt(pages)}</span>
      <div className="viz-pager"><button type="button" className="btn" disabled={current === 0} onClick={() => setPage(current - 1)}>← Previous</button><button type="button" className="btn" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>Next →</button></div>
    </div>
    <div className="viz-gallery-grid">{visible.map((i) => {
      const id = ds.p.id[i], url = thumbs[id], codes = photoCodes(colorBy, i);
      return <button type="button" key={id} className={`viz-thumb ${atlas.selectedPhoto === i ? 'is-active' : ''}`} onClick={() => atlas.selectPhoto(i)} aria-label={`Inspect photo ${ds.p.code[i]}`}>
        <span className="viz-thumb-image">{url ? <img src={url} alt="" loading="lazy" /> : <span>{url === '' ? 'No image' : 'Loading…'}</span>}</span>
        <span className="viz-thumb-meta"><b>{ds.p.code[i]}</b><span>{ds.snapshot.streets[ds.p.street[i]]}</span></span>
        <span className="viz-thumb-tags">{codes.slice(0, 3).map((k) => <span key={k}><Swatch color={colorBy.categories[k].color} hollow={colorBy.categories[k].structural} />{colorBy.categories[k].label}</span>)}{codes.length > 3 && <span>+{codes.length - 3}</span>}</span>
        <span className="viz-thumb-date">{ds.p.imagery_month[i] >= 0 ? `Imagery ${monthLabel(ds.p.imagery_month[i])}` : 'Imagery undated'}</span>
      </button>;
    })}</div>
    {!list.length && <div className="viz-empty"><h2>No photos match</h2><p>Clear a filter to see photos again.</p></div>}
  </div>;
}
