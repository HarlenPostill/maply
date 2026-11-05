import { useEffect, useState } from 'react';
import type { AccessibilityFeature } from './api';

const PALETTES: Record<AccessibilityFeature['kind'], string[]> = {
  footpath_section: ['#087c64', '#00a888', '#065c52'],
  driveway: ['#277bd0', '#104991', '#6197ea'],
  kerb_ramp: ['#d17800', '#ad4d00', '#edab0b'],
  crossing: ['#9a56da', '#6733a9', '#c077ec'],
  hazard: ['#db4657', '#aa173c', '#ee7478'],
};
const NAMES: Record<AccessibilityFeature['kind'], string> = { footpath_section: 'Footpath', driveway: 'Driveway', kerb_ramp: 'Kerb ramp', crossing: 'Crossing', hazard: 'Hazard' };

export function featureStyles(features: AccessibilityFeature[]) {
  const counts: Partial<Record<AccessibilityFeature['kind'], number>> = {};
  return features.map((feature, index) => {
    const occurrence = counts[feature.kind] ?? 0;
    counts[feature.kind] = occurrence + 1;
    return { feature, number: index + 1, color: PALETTES[feature.kind][occurrence % PALETTES[feature.kind].length], label: `${NAMES[feature.kind]} ${occurrence + 1}` };
  });
}

export function ImageRegions({ imageUrl, code, features, focused, onFocus, imageRef }: {
  imageUrl: string; code: string; features: AccessibilityFeature[];
  focused: string | null; onFocus: (feature: AccessibilityFeature | null) => void;
  imageRef: React.RefObject<HTMLDivElement>;
}) {
  const [showRegions, setShowRegions] = useState(true);
  useEffect(() => { if (focused) setShowRegions(true); }, [focused]);
  const styles = featureStyles(features);
  const regions = styles.filter((item) => item.feature.region);
  return <div className="image-regions">
    <div className="feature-image" ref={imageRef}>
      <a href={imageUrl} target="_blank" rel="noreferrer"><img className="detail-img" src={imageUrl} alt={`Street View photo ${code}`} /></a>
      {showRegions && regions.map(({ feature, number, color, label }) => {
        const [x1, y1, x2, y2] = feature.region!;
        return <button type="button" key={feature.id} className={`feature-region ${focused === feature.id ? 'region-focused' : ''}`} aria-label={`Region ${number}: ${label}. ${feature.location_description}`} aria-pressed={focused === feature.id} title={feature.location_description} onClick={() => onFocus(focused === feature.id ? null : feature)} style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%`, borderColor: color, backgroundColor: `${color}0d`, opacity: focused && focused !== feature.id ? .45 : 1, zIndex: focused === feature.id ? 3 : 2 }}><span style={{ background: color }}>{number}</span></button>;
      })}
    </div>
    {regions.length > 0 && <>
      <div className="region-controls"><label className="check"><input type="checkbox" checked={showRegions} onChange={(e) => setShowRegions(e.target.checked)} />Show feature regions ({regions.length})</label>{focused && <button type="button" className="btn btn-quiet" onClick={() => onFocus(null)}>Clear focus</button>}</div>
      <ul className="region-legend" aria-label="Image feature legend">{styles.map(({ feature, number, color, label }) => <li key={feature.id}><button type="button" disabled={!feature.region} aria-pressed={focused === feature.id} onClick={() => { setShowRegions(true); onFocus(focused === feature.id ? null : feature); }} title={feature.location_description}><span style={{ background: color }}>{number}</span>{label}{!feature.region && <small>no region</small>}</button></li>)}</ul>
    </>}
    <a className="image-original" href={imageUrl} target="_blank" rel="noreferrer">Open full-size original</a>
  </div>;
}
