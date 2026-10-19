"""Descriptive model confidence, never calibrated accuracy or route safety."""
import math


def confidence_for_labels(labels: dict) -> dict:
    observations = [labels.get('footpath', {})]
    observations.extend(tag for feature in labels.get('features', []) for tag in feature.get('tags', {}).values())
    scores, unknown = [], 0
    for observation in observations:
        score = observation.get('confidence')
        if observation.get('value') == 'unknown' or not isinstance(score, (float, int)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
            unknown += 1
        else:
            scores.append(score)
    return {'mean': sum(scores) / len(scores) if scores else None, 'minimum': min(scores) if scores else None,
            'known': len(scores), 'unknown': unknown}


def aggregate_confidence(metrics: list[dict]) -> dict:
    scores = [m['mean'] for m in metrics if m.get('mean') is not None]
    minima = [m['minimum'] for m in metrics if m.get('minimum') is not None]
    return {'mean': sum(scores) / len(scores) if scores else None, 'minimum': min(minima) if minima else None,
            'known': sum(m.get('known', 0) for m in metrics), 'unknown': sum(m.get('unknown', 0) for m in metrics), 'images': len(metrics)}
