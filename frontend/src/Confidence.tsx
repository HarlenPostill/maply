import type { AccessibilityLabels, ConfidenceSummary } from './api';

export function imageConfidence(labels: AccessibilityLabels | null): ConfidenceSummary | null {
  if (!labels) return null;
  const tags = [labels.footpath, ...labels.features.flatMap((f) => Object.values(f.tags))];
  const known = tags.filter((tag) => tag.confidence !== null).map((tag) => tag.confidence!);
  return { mean: known.length ? known.reduce((sum, n) => sum + n, 0) / known.length : null, minimum: known.length ? Math.min(...known) : null, known: known.length, unknown: tags.length - known.length, images: 1 };
}

export const confidencePercent = (value: number | null | undefined) => value == null ? '—' : `${Math.round(value * 100)}%`;

export function ConfidenceCard({ confidence, images, eligible, aggregate = false }: { confidence: ConfidenceSummary | null | undefined; images?: number; eligible?: number; aggregate?: boolean }) {
  return <div className="confidence-card">
    <div><strong>{confidencePercent(confidence?.mean)}</strong><span>Mean model confidence</span></div>
    <p>{confidence ? <>{confidence.known.toLocaleString()} known · {confidence.unknown.toLocaleString()} unknown observations{confidence.minimum != null && <> · lowest {confidencePercent(confidence.minimum)}</>}</> : 'No observations yet'}</p>
    {eligible !== undefined && <p>{(confidence?.images ?? images ?? 0).toLocaleString()} / {eligible.toLocaleString()} saved images with observations</p>}
    <small>{aggregate ? 'Mean of tagged image scores. ' : 'Mean of known tag confidences. '}Unknowns are excluded. Model confidence is not measured accuracy or an accessibility rating.</small>
  </div>;
}
