import { useEffect, useMemo, useRef, useState } from 'react';
import { api, type AccessibilityFeature, type Shot } from '../api';
import { ImageRegions } from '../ImageRegions';
import type { Atlas, Group } from './context';
import { BarList, Swatch } from './charts';
import { fmt, minuteDate, monthLabel, photoCategory, tally, words, type Dataset, type Selection } from './model';

function subset(ds: Dataset, photos: number[], sel: Selection): Selection {
  const photo = new Uint8Array(ds.n);
  for (const i of photos) photo[i] = 1;
  const feature = new Uint8Array(ds.fn);
  let features = 0;
  for (let f = 0; f < ds.fn; f++) if (photo[ds.fPhoto[f]] && sel.feature[f]) { feature[f] = 1; features++; }
  return { photo, feature, photos: photos.length, features };
}
const when = (minutes: number) => minutes >= 0 ? minuteDate(minutes).toLocaleString('en-AU', { dateStyle: 'medium', timeStyle: 'short' }) : 'Not recorded';

function GroupPanel({ atlas, group }: { atlas: Atlas; group: Group }) {
  const { ds, colorBy } = atlas;
  const sel = useMemo(() => subset(ds, group.photos, atlas.sel), [ds, group, atlas.sel]);
  const t = useMemo(() => tally(ds, colorBy, sel, 'photos'), [ds, colorBy, sel]);
  const [limit, setLimit] = useState(60);
  return <>
    <h2>{group.title}</h2><p className="viz-fine">{group.subtitle} · {fmt(group.photos.length)} matching photos · {fmt(sel.features)} features</p>
    <h4 className="viz-detail-sub">{colorBy.label}</h4>
    <BarList dim={colorBy} tally={t} compact limit={8} />
    <h4 className="viz-detail-sub">Photos</h4>
    <div className="viz-photo-chips">{group.photos.slice(0, limit).map((i) => { const k = photoCategory(colorBy, i); const c = colorBy.categories[k];
      return <button type="button" key={i} onClick={() => atlas.selectPhoto(i)} title={c?.label}>{c && <Swatch color={c.color} hollow={c.structural} />}{ds.p.code[i]}</button>; })}</div>
    {limit < group.photos.length && <button type="button" className="viz-text-button" onClick={() => setLimit(limit + 120)}>Show more ({fmt(group.photos.length - limit)} remaining)</button>}
  </>;
}

function PhotoPanel({ atlas, index, siblings }: { atlas: Atlas; index: number; siblings: number[] }) {
  const { ds, colorBy } = atlas;
  const id = ds.p.id[index];
  const [shot, setShot] = useState<Shot | null>(null);
  const [error, setError] = useState('');
  const [focused, setFocused] = useState<string | null>(null);
  const imageRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false; setShot(null); setError(''); setFocused(null);
    api.get<Shot>(`/api/shots/${id}`).then((value) => { if (!disposed) setShot(value); }).catch((e: Error) => { if (!disposed) setError(e.message); });
    return () => { disposed = true; };
  }, [id, ds.snapshot.digest]);
  const position = siblings.indexOf(index);
  const step = (delta: number) => { const next = siblings[(position + delta + siblings.length) % siblings.length]; if (next !== undefined) atlas.selectPhoto(next); };
  const activeTag = colorBy.id.startsWith('tag:') ? colorBy.id.split(':')[2] : null;
  const activeKind = colorBy.id.startsWith('tag:') ? colorBy.id.split(':')[1] : null;
  const month = ds.p.imagery_month[index];
  const dateStatus = atlas.dimMap.get('imagery_date_status')!;
  const statusLabel = dateStatus.categories[dateStatus.data().single![index]]?.label;
  const labels = shot?.labels;
  const current = labels && (labels.provenance as Record<string, unknown> | undefined)?.image_sha256 === shot?.result?.sha256;
  const k = photoCategory(colorBy, index);
  return <>
    {siblings.length > 1 && <div className="viz-detail-nav"><button type="button" className="btn" onClick={() => step(-1)} aria-label="Previous photo">←</button><span>{position >= 0 ? `${fmt(position + 1)} of ${fmt(siblings.length)}` : `${fmt(siblings.length)} photos`}</span><button type="button" className="btn" onClick={() => step(1)} aria-label="Next photo">→</button></div>}
    <h2>{ds.snapshot.streets[ds.p.street[index]]}</h2>
    <div className="viz-photo-meta"><strong>{ds.p.code[index]}</strong><span>{words(ds.vocab.sides[ds.p.side[index]] ?? 'none')} side{shot?.footpath ? ` · ${shot.footpath} footpath` : ''}</span></div>
    {k >= 0 && <p className="viz-chip-line"><Swatch color={colorBy.categories[k].color} hollow={colorBy.categories[k].structural} />{colorBy.label}: <b>{colorBy.categories[k].label}</b></p>}
    {error && <div className="error">{error}</div>}
    {!shot && !error && <div className="viz-photo-placeholder">Loading evidence…</div>}
    {shot && shot.image_url && (labels?.features?.length ? <ImageRegions imageUrl={shot.image_url} code={shot.code} features={labels.features} focused={focused} onFocus={(f: AccessibilityFeature | null) => setFocused(f?.id ?? null)} imageRef={imageRef} />
      : <a href={shot.image_url} target="_blank" rel="noreferrer"><img className="viz-evidence-photo" src={shot.thumb_url || shot.image_url} alt={`${shot.street}, ${shot.side} side, source photo ${shot.code}`} /></a>)}
    {shot && !shot.image_url && <p className="viz-photo-placeholder">No saved image ({words(shot.status)}).</p>}
    {shot?.image_url && <a className="viz-text-button" href={shot.image_url} target="_blank" rel="noreferrer">Open full-size source ↗</a>}
    <dl className="viz-facts">
      <dt>Imagery month</dt><dd>{month >= 0 ? monthLabel(month) : statusLabel}</dd>
      <dt>Captured</dt><dd>{when(ds.p.captured[index])}{ds.p.capture_source[index] >= 0 ? ` · ${ds.vocab.capture_source[ds.p.capture_source[index]]}` : ''}</dd>
      <dt>Annotated</dt><dd>{when(ds.p.tagged[index])}{ds.p.model[index] >= 0 ? ` · ${ds.vocab.model[ds.p.model[index]]}` : ''}</dd>
      <dt>Review</dt><dd>{ds.p.review_confirmation[index] >= 0 ? words(ds.vocab.review_confirmation[ds.p.review_confirmation[index]]) + ' confirmation' : ds.p.annotation[index] === 1 ? 'Model annotation only' : 'Not annotated'}{ds.p.review_reasons[index] > 0 ? ` · ${ds.p.review_reasons[index]} notes` : ''}</dd>
      <dt>Panorama</dt><dd>{ds.panoSize[index] > 1 ? `Shared with ${ds.panoSize[index] - 1} other photo${ds.panoSize[index] > 2 ? 's' : ''}` : ds.panoSize[index] === 1 ? 'Only photo from this panorama' : 'No panorama id'}</dd>
      <dt>Position</dt><dd>{ds.p.position[index] ? 'Planned (fallback)' : 'Observed camera'} {ds.p.lat[index].toFixed(6)}, {ds.p.lng[index].toFixed(6)}</dd>
    </dl>
    <p className="viz-fine">The position is the camera, not a surveyed feature location. Imagery may predate capture.</p>
    {labels && !current && <p className="viz-fine">These tags belong to an older image and are not counted in the atlas.</p>}
    {labels && <>
      <p className="viz-footpath-line"><strong>Footpath:</strong> {words(labels.footpath.value)} <small>{labels.footpath.visibility}{labels.footpath.confidence !== null ? ` · ${Math.round(labels.footpath.confidence * 100)}%` : ''}</small></p>
      <p className="viz-fine">{labels.footpath.evidence}</p>
      {labels.features.map((feature, n) => <details className={`viz-feature ${focused === feature.id ? 'is-focused' : ''}`} key={feature.id} open={feature.kind === activeKind || focused === feature.id}>
        <summary onMouseEnter={() => setFocused(feature.id)}>{n + 1}. {words(feature.kind)} · {feature.location_description}</summary>
        {Object.entries(feature.tags).map(([name, tag]) => <div className={`viz-tag ${name === activeTag && feature.kind === activeKind ? 'is-active' : ''}`} key={name}>
          <strong>{words(name)}: {words(tag.value)}</strong><span>{tag.evidence}</span>
          <small>{tag.visibility} · {tag.confidence === null ? 'confidence n/a' : `${Math.round(tag.confidence * 100)}% model confidence`}</small></div>)}
      </details>)}
      {labels.review_reasons.length > 0 && <details className="viz-feature"><summary>{labels.review_reasons.length} review notes</summary><ul>{labels.review_reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></details>}
    </>}
  </>;
}

export default function Evidence({ atlas, onClose }: { atlas: Atlas; onClose: () => void }) {
  const siblings = useMemo(() => {
    if (atlas.group) return atlas.group.photos;
    const out: number[] = [];
    if (atlas.selectedPhoto === null) return out;
    const seg = atlas.ds.p.segment[atlas.selectedPhoto];
    for (let i = 0; i < atlas.ds.n; i++) if (atlas.sel.photo[i] && atlas.ds.p.segment[i] === seg) out.push(i);
    return out;
  }, [atlas.group, atlas.selectedPhoto, atlas.ds, atlas.sel]);
  return <aside className="viz-detail" aria-label="Evidence inspector">
    <div className="viz-detail-head"><span className="viz-eyebrow">{atlas.selectedPhoto !== null ? 'SOURCE PHOTO' : 'SELECTION'}</span>
      <div>{atlas.selectedPhoto !== null && atlas.group && <button type="button" className="viz-text-button" onClick={() => atlas.selectPhoto(null)}>← {atlas.group.title}</button>}
        <button type="button" className="viz-text-button" onClick={onClose} aria-label="Close evidence">Close ×</button></div></div>
    {atlas.selectedPhoto !== null ? <PhotoPanel key={atlas.ds.p.id[atlas.selectedPhoto]} atlas={atlas} index={atlas.selectedPhoto} siblings={siblings} />
      : atlas.group ? <GroupPanel atlas={atlas} group={atlas.group} /> : null}
  </aside>;
}
