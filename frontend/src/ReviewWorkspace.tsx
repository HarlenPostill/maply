import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { ImageRegions } from './ImageRegions';
import type { AccessibilityFeature, JobSummary } from './api';
import { batchSchema, errorMessage, pageSchema, resultSchema, reviewRequest, summarySchema, type ReviewBatch, type ReviewItem, type ReviewLabels, type ReviewPage, type ReviewQueue, type ReviewSummary } from './reviewApi';
import './review.css';

const QUEUES: { key: ReviewQueue; label: string; description: string; group: 'review' | 'recover' | 'done' }[] = [
  { key: 'absence', label: 'No footpath', description: 'Double-check the absence decisions', group: 'review' },
  { key: 'untagged', label: 'Photos to tag', description: 'New saved photos ready for an AI annotation proposal', group: 'review' },
  { key: 'ready', label: 'Ready to confirm', description: 'AI second opinions waiting for you', group: 'review' },
  { key: 'needs_check', label: 'Needs a look', description: 'Inspect images and their quality checks', group: 'review' },
  { key: 'skipped', label: 'Skipped', description: 'Find a usable view for these points', group: 'recover' },
  { key: 'no_imagery', label: 'No imagery', description: 'Check whether another view is available', group: 'recover' },
  { key: 'blocked', label: 'Blocked', description: 'Find a clear view of the intended footpath', group: 'recover' },
  { key: 'recovery', label: 'Capture queued', description: 'Prepared for the local capture extension', group: 'recover' },
  { key: 'confirmed', label: 'Confirmed', description: 'Second reviews you have accepted', group: 'done' },
];
const PAGE_SIZE = 24;
function words(value: string) { return value.replaceAll('_', ' '); }
function number(value: number) { return value.toLocaleString(); }
function percent(value: number | null) { return value === null ? 'Not given' : `${Math.round(value * 100)}%`; }

export default function ReviewWorkspace({ job }: { job: JobSummary }) {
  const [queue, setQueue] = useState<ReviewQueue>('absence');
  const [offset, setOffset] = useState(0);
  const [summary, setSummary] = useState<ReviewSummary | null>(null);
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(20);
  const [concurrency, setConcurrency] = useState(2);
  const selected = page?.items.find((item) => item.id === selectedId) ?? page?.items[0] ?? null;
  const definition = QUEUES.find((item) => item.key === queue);
  const activeBatch = summary?.batches.find((batch) => batch.status === 'running' || batch.status === 'paused');

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      reviewRequest(`/api/jobs/${job.id}/review/summary`, summarySchema, { signal: controller.signal }),
      reviewRequest(`/api/jobs/${job.id}/review/items?queue=${queue}&offset=${offset}&limit=${PAGE_SIZE}`, pageSchema, { signal: controller.signal }),
    ]).then(([nextSummary, nextPage]) => {
      if (controller.signal.aborted) return;
      setSummary(nextSummary); setPage(nextPage); setError(''); setLoading(false);
      if (!nextPage.items.length && offset > 0) setOffset(Math.max(0, offset - PAGE_SIZE));
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) { setError(errorMessage(failure)); setLoading(false); }
    });
    return () => controller.abort();
  }, [job.id, queue, offset, revision]);
  useEffect(() => {
    const timer = window.setInterval(() => setRevision((value) => value + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);

  function chooseQueue(next: ReviewQueue) {
    setQueue(next); setOffset(0); setPage(null); setSelectedId(null); setLoading(true); setNotice('');
  }
  async function perform(action: () => Promise<unknown>, message: string) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); setNotice(message); setRevision((value) => value + 1); }
    catch (failure: unknown) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  function decide(item: ReviewItem, decision: 'confirm' | 'reject' | 'needs_capture') {
    const review = item.review;
    if (!review) return;
    const message = decision === 'confirm' ? 'Review confirmed. Other capture checks remain available for inspection.' : decision === 'reject' ? 'Second opinion rejected. The existing annotation is unchanged and remains unconfirmed.' : 'Point prepared for another photo. Find it in Capture queued.';
    return perform(() => reviewRequest(`/api/review/items/${review.id}/decision`, resultSchema, { body: { decision } }), message);
  }
  function recover(item: ReviewItem) {
    return perform(() => reviewRequest(`/api/review/shots/${item.id}/recover`, resultSchema, { body: {} }), 'Point prepared for another photo. Use the local Maply extension, then return here to review it.');
  }
  function tagPhoto(item: ReviewItem) {
    return perform(() => reviewRequest(`/api/review/shots/${item.id}/tag`, batchSchema, { body: {} }), 'One-photo AI review started. Inspect the proposal in Ready to confirm before accepting it.');
  }
  function controlBatch(batch: ReviewBatch, action: 'pause' | 'resume' | 'cancel') {
    return perform(() => reviewRequest(`/api/review/batches/${batch.id}/${action}`, batchSchema, { body: {} }), action === 'pause' ? 'New AI requests paused. Requests already in flight can still finish.' : action === 'resume' ? 'AI review resumed.' : 'Remaining AI review work cancelled. Completed proposals are retained.');
  }

  return <div className="review-workspace">
    <aside className="review-sidebar" aria-label="Review queues">
      <div className="review-project"><span className="review-eyebrow">Your dataset</span><h2>{job.name}</h2><p>{number(job.counts.total)} planned points</p></div>
      {(['review', 'recover', 'done'] satisfies ('review' | 'recover' | 'done')[]).map((group) => <section className="review-queue-group" key={group}>
        <h3>{group === 'review' ? 'Review observations' : group === 'recover' ? 'Recover missing views' : 'Your decisions'}</h3>
        {QUEUES.filter((item) => item.group === group).map((item) => <button type="button" key={item.key} className="review-queue" aria-current={queue === item.key ? 'page' : undefined} onClick={() => chooseQueue(item.key)}>
          <span>{item.label}</span><strong>{summary ? number(summary.counts[item.key] ?? 0) : '—'}</strong>
        </button>)}
      </section>)}
      <p className="review-sidebar-note">Batch reviews automatically accept the AI’s result. An unknown answer stays unknown; missing views still need imagery.</p>
    </aside>
    <main className="review-main">
      <header className="review-section-head"><div><span className="review-eyebrow">Dataset review</span><h1>{definition?.label ?? 'Review'}</h1><p>{definition?.description}</p></div><button className="btn btn-quiet" onClick={() => setRevision((value) => value + 1)} disabled={busy}>Refresh</button></header>
      {queue === 'absence' && <section className="review-start" aria-label="Start AI second review">
        <div><h2>A fresh look at “no footpath”</h2><p>The AI examines each original photo again and saves its result automatically. No second manual review is needed.</p><small>Model: {summary?.provider.model ?? 'Loading…'}{summary?.counts.absence_only !== undefined ? ` · ${number(summary.counts.absence_only)} points have only the absence check` : ''}</small></div>
        <form onSubmit={(event) => { event.preventDefault(); void perform(() => reviewRequest(`/api/jobs/${job.id}/review/batches`, batchSchema, { body: { limit, concurrency, auto_confirm: true } }), 'AI second review started. Results will be saved automatically in Confirmed.'); }}>
          <label>Photos<input aria-label="Photos per AI review batch" type="number" min={1} max={5000} required value={limit} onChange={(event) => setLimit(Number(event.target.value))} /></label>
          <label>At a time<select aria-label="Parallel AI reviews" value={concurrency} onChange={(event) => setConcurrency(Number(event.target.value))}>{Array.from({ length: 20 }, (_, index) => index + 1).map((n) => <option key={n} value={n}>{n}</option>)}</select></label>
          <button type="submit" className="btn btn-primary" disabled={busy || !summary?.provider.configured || !!activeBatch || !Number.isInteger(limit) || limit < 1 || limit > 5000}>Review next {number(limit || 0)} with AI</button>
          <p className="review-paid-note">Up to {number(limit || 0)} paid OpenRouter requests. AI results are accepted automatically, including unknown answers.{summary && !summary.provider.configured ? ' Add an API key in Model settings first.' : ''}</p>
        </form>
      </section>}
      {activeBatch && <BatchProgress batch={activeBatch} busy={busy} onAction={controlBatch} />}
      {!activeBatch && summary?.batches[0] && <BatchProgress batch={summary.batches[0]} busy={busy} onAction={controlBatch} />}
      {error && <div className="error" role="alert">{error}</div>}
      {notice && <div className="review-notice" role="status">{notice}</div>}
      <div className="review-desk">
        <section className="review-list" aria-label={`${definition?.label ?? 'Review'} photos`}>
          <div className="review-list-head"><strong>{page ? number(page.total) : '…'} points</strong><span>{page?.total ? `${offset + 1}–${offset + page.items.length}` : ''}</span></div>
          {loading && !page ? <p className="review-empty">Loading this queue…</p> : page?.items.length ? <ul>{page.items.map((item) => <li key={item.id}><button className="review-list-item" aria-pressed={selected?.id === item.id} onClick={() => setSelectedId(item.id)}>
            {item.thumb_url || item.review_image_url ? <img src={item.thumb_url ?? item.review_image_url ?? undefined} alt="" loading="lazy" /> : <span className="review-no-thumb">No saved<br />photo</span>}
            <span><strong>{item.street || 'Unnamed street'}</strong><small>{item.code} · {item.footpath || words(item.side)}</small><em>{item.review ? words(item.review.status) : item.image_url ? 'Awaiting review' : words(item.status)}</em></span>
          </button></li>)}</ul> : <p className="review-empty">{error ? 'The queue could not be loaded.' : 'No points in this queue.'}</p>}
          <div className="review-pagination"><button className="btn btn-quiet" disabled={offset === 0 || busy} onClick={() => { setOffset(Math.max(0, offset - PAGE_SIZE)); setSelectedId(null); }}>Previous</button><button className="btn btn-quiet" disabled={!page || offset + page.items.length >= page.total || busy} onClick={() => { setOffset(offset + PAGE_SIZE); setSelectedId(null); }}>Next</button></div>
        </section>
        {selected ? <Inspector key={selected.id} item={selected} busy={busy} aiAvailable={!!summary?.provider.configured && !activeBatch} model={summary?.provider.model ?? ''} onTag={tagPhoto} onDecide={decide} onRecover={recover} onAcceptPhoto={(item) => perform(() => api.patch(`/api/shots/${item.id}`, { status: 'accepted' }), 'Photo marked as checked. Its annotation has not been changed.')} /> : <section className="review-inspector review-inspector-empty"><span className="review-empty-symbol">✓</span><h2>{loading ? 'Preparing your review' : 'Nothing waiting here'}</h2><p>{loading ? 'The latest observations are loading.' : 'Choose another queue to continue checking the dataset.'}</p></section>}
      </div>
    </main>
  </div>;
}

function BatchProgress({ batch, busy, onAction }: { batch: ReviewBatch; busy: boolean; onAction: (batch: ReviewBatch, action: 'pause' | 'resume' | 'cancel') => unknown }) {
  const waiting = (batch.counts.queued ?? 0) + (batch.counts.running ?? 0);
  const total = Object.values(batch.counts).reduce((sum, value) => sum + value, 0);
  const completed = total - waiting;
  return <section className="review-batch" aria-label="AI review progress">
    <div className="review-batch-title"><strong>AI second review · {words(batch.status)}</strong><span>{number(completed)} of {number(total)} processed</span></div>
    <progress aria-label="AI review completion" value={completed} max={Math.max(total, 1)} />
    <div className="review-batch-bottom"><p>{number(batch.counts.confirmed ?? 0)} confirmed · {number(batch.counts.ready ?? 0)} awaiting a decision · {number(batch.counts.running ?? 0)} reviewing · {number(batch.counts.failed ?? 0)} failed <small>{batch.model} · {batch.auto_confirm ? 'Automatic acceptance' : 'Manual confirmation'}</small></p><div>{batch.status === 'running' && <button className="btn" disabled={busy} onClick={() => onAction(batch, 'pause')}>Pause</button>}{batch.status === 'paused' && <button className="btn" disabled={busy} onClick={() => onAction(batch, 'resume')}>Resume AI review</button>}{['running', 'paused'].includes(batch.status) && <button className="btn btn-quiet" disabled={busy} onClick={() => onAction(batch, 'cancel')}>Cancel remaining</button>}</div></div>
  </section>;
}

function Inspector({ item, busy, aiAvailable, model, onTag, onDecide, onRecover, onAcceptPhoto }: { item: ReviewItem; busy: boolean; aiAvailable: boolean; model: string; onTag: (item: ReviewItem) => unknown; onDecide: (item: ReviewItem, decision: 'confirm' | 'reject' | 'needs_capture') => unknown; onRecover: (item: ReviewItem) => unknown; onAcceptPhoto: (item: ReviewItem) => unknown }) {
  const imageRef = useRef<HTMLDivElement>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const [showProposal, setShowProposal] = useState(false);
  const [copyNotice, setCopyNotice] = useState('');
  const reviewInProgress = !!item.review && ['queued', 'running', 'ready'].includes(item.review.status);
  const canRequestAI = ['captured', 'accepted', 'needs_check'].includes(item.status) && !reviewInProgress && item.review?.status !== 'confirmed';
  async function copyCode() {
    try { await navigator.clipboard.writeText(item.code); setCopyNotice('Copied'); }
    catch { setCopyNotice('Select and copy the code above'); }
  }
  const proposal = item.review?.proposed_labels;
  const isReady = item.review?.status === 'ready' && !!proposal;
  const imageLabels = showProposal && proposal ? proposal : item.labels;
  const reason = typeof item.result.manual_review_reason === 'string' ? item.result.manual_review_reason : null;
  const canConfirm = isReady && proposal.footpath.value !== 'unknown';
  const hasImage = !!item.image_url;
  return <section className="review-inspector" aria-label={`Review ${item.code}`}>
    <header className="review-photo-head"><div><span className="review-eyebrow">{item.code} · {words(item.status)}</span><h2>{item.street || 'Unnamed street'}</h2><p>{item.footpath ? `${item.footpath} footpath` : `${words(item.side)} side`} · requested heading {Math.round(item.heading)}°</p></div><a className="btn" href={item.url} target="_blank" rel="noreferrer">Open Street View ↗</a></header>
    {hasImage && item.image_url ? <div className="review-photo"><ImageRegions imageUrl={item.image_url} code={item.code} features={imageLabels?.features ?? []} focused={focused} onFocus={(feature: AccessibilityFeature | null) => setFocused(feature?.id ?? null)} imageRef={imageRef} />{proposal && <label className="review-region-toggle"><input type="checkbox" checked={showProposal} onChange={(event) => { setShowProposal(event.target.checked); setFocused(null); }} />Show regions from the AI second review</label>}</div> : <div className="review-missing-photo">{item.review_image_url && <img src={item.review_image_url} alt={`Unaccepted capture evidence for ${item.code}`} />}<div><h3>{item.review_image_url ? 'Capture evidence, not a saved data photo' : 'No saved photo for this point'}</h3><p>Open the intended Street View position and find a usable view. A skipped or unavailable image does not establish that a footpath is absent.</p><button className="btn btn-primary" disabled={busy || item.status === 'retake'} onClick={() => onRecover(item)}>{item.status === 'retake' ? 'Queued for capture' : 'Prepare for capture'}</button><p className="fineprint">Open the local Maply Chrome extension, paste the photo code into “Start at photo code”, then choose Start recovery. Return to Photos to tag when the saved photo arrives.</p></div></div>}
    {(item.status === 'retake' || item.status === 'pending') && <div className="review-recovery-code"><span>Capture this point: <code>{item.code}</code></span><button className="btn" onClick={() => void copyCode()}>Copy photo code</button><small role="status">{copyNotice}</small><p>In the local Maply extension, paste this code into “Start at photo code” and choose Start recovery. The new image will appear in Photos to tag.</p></div>}
    {(reason || item.flags.length > 0 || item.note) && <details className="review-checks" open={!!reason}><summary>Why this point needs attention</summary>{reason && <p>{reason}</p>}{item.note && <p>{item.note}</p>}{item.flags.length > 0 && <ul>{item.flags.map((flag) => <li key={flag}>{words(flag)}</li>)}</ul>}</details>}
    {hasImage && <div className="review-comparison"><Annotation title="Current annotation" labels={item.labels} /><Annotation title="AI second review" labels={proposal ?? null} model={item.review?.model} state={item.review?.status} error={item.review?.error} /></div>}
    {hasImage && <footer className="review-decision">
      {isReady ? <><div><h3>{proposal.footpath.value === 'absent' ? 'Is the intended footpath really absent?' : proposal.footpath.value === 'present' ? 'The AI found a footpath. Check the proposed tags.' : 'The AI could not establish an answer.'}</h3><p>{canConfirm ? 'Confirm only after checking the image and the proposed annotation. Other capture issues stay on the point.' : 'An uncertain answer cannot be confirmed as real data. Keep the point unresolved or get another photo.'}</p></div><div className="review-decision-buttons"><button className="btn btn-primary" disabled={busy || !canConfirm} onClick={() => onDecide(item, 'confirm')}>{proposal.footpath.value === 'absent' ? 'Confirm no footpath' : 'Confirm reviewed annotation'}</button><button className="btn" disabled={busy} onClick={() => onDecide(item, 'reject')}>Reject second opinion</button><button className="btn btn-quiet" disabled={busy} onClick={() => onDecide(item, 'needs_capture')}>Needs another photo</button></div></> : <><div><h3>{item.review?.status === 'confirmed' ? 'Review result accepted' : item.review?.status === 'stale' ? 'The source changed after this review' : 'Inspect the original evidence'}</h3><p>{item.review?.status === 'confirmed' ? 'The confirmed annotation is attached to this photo. Remaining capture flags still need separate attention.' : item.labels?.footpath.value === 'absent' ? 'Batch reviews save results automatically. A single-photo review lets you compare the proposed tags before confirming.' : 'Request a fresh annotation proposal, or inspect the photo directly. Mark photo checked clears its capture checks: use it only after verifying the intended side, position and every listed issue.'}</p></div><div className="review-decision-buttons">{canRequestAI && <button className="btn btn-primary" disabled={busy || !aiAvailable} onClick={() => onTag(item)}>Request AI review · 1 photo</button>}{item.status === 'needs_check' && item.labels?.footpath.value === 'present' && <button className="btn" disabled={busy} onClick={() => onAcceptPhoto(item)}>Mark photo checked</button>}<button className="btn btn-quiet" disabled={busy} onClick={() => onRecover(item)}>Needs another photo</button></div></>}
      {!isReady && canRequestAI && <p className="review-paid-note">One paid OpenRouter request using {model || 'your configured model'}. You must confirm the result.{!aiAvailable ? ' Configure a model key, and finish or cancel any active batch first.' : ''}</p>}
    </footer>}
  </section>;
}

function Annotation({ title, labels, model, state, error }: { title: string; labels: ReviewLabels | null; model?: string; state?: string; error?: string | null }) {
  return <section className="review-annotation"><h3>{title}</h3>{labels ? <><span className={`review-verdict verdict-${labels.footpath.value}`}>{labels.footpath.value === 'absent' ? 'No footpath visible' : labels.footpath.value === 'present' ? 'Footpath present' : 'Cannot determine'}</span><p>{labels.footpath.evidence}</p><div className="review-confidence"><span>Model-reported confidence</span><strong>{percent(labels.footpath.confidence)}</strong></div><small>Visibility: {words(labels.footpath.visibility)}</small>{labels.review_reasons.length > 0 && <ul className="review-reasons">{labels.review_reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}</ul>}{labels.features.length > 0 && <details><summary>{labels.features.length} tagged features</summary>{labels.features.map((feature) => <div className="review-feature" key={feature.id}><strong>{words(feature.kind)}</strong><p>{feature.location_description}</p><dl>{Object.entries(feature.tags).map(([name, tag]) => <div key={name}><dt>{words(name)}</dt><dd>{words(tag.value)}<small>{tag.evidence}</small></dd></div>)}</dl></div>)}</details>}{labels.notes.length > 0 && <details><summary>Annotation notes</summary>{labels.notes.map((note, index) => <p key={`${index}-${note}`}>{note}</p>)}</details>}</> : <p className="review-annotation-empty">{state === 'running' ? 'The model is examining this photo…' : state === 'queued' ? 'Queued for a fresh look.' : state === 'failed' ? 'The AI review did not complete.' : title === 'Current annotation' ? 'This photo has not been tagged yet.' : 'No second opinion yet. The original annotation is preserved until you confirm a review.'}</p>}{error && <p className="error">{error}</p>}{model && <small className="review-model">{model}</small>}</section>;
}
