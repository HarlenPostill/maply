"""Confidence summaries preserve uncertainty and weight observations explicitly."""

import pytest


def score(value, confidence):
    return {"value": value, "confidence": confidence, "evidence": "Visible evidence", "visibility": "clear"}


def test_confidence_includes_overall_presence_and_every_feature_tag():
    from app.tagging import confidence_for_labels

    result = confidence_for_labels({
        "footpath": score("present", 0.9),
        "features": [
            {"tags": {"surface": score("concrete", 0.7), "slope": score("unknown", None)}},
            {"tags": {"type": score("step", 0.5)}},
        ],
        # Metadata numbers must not affect observed label confidence.
        "provenance": {"attempt": 100, "confidence": 1.0},
    })
    assert result["mean"] == pytest.approx(0.7)
    assert result["minimum"] == 0.5
    assert result["known"] == 3 and result["unknown"] == 1


def test_unknown_only_observations_have_no_numeric_score():
    from app.tagging import confidence_for_labels

    assert confidence_for_labels({"footpath": score("unknown", None), "features": []}) == {
        "mean": None, "minimum": None, "known": 0, "unknown": 1,
    }


def test_absent_footpath_confidence_is_still_a_known_observation():
    from app.tagging import confidence_for_labels

    assert confidence_for_labels({"footpath": score("absent", 0.84), "features": []}) == {
        "mean": 0.84, "minimum": 0.84, "known": 1, "unknown": 0,
    }


def test_zero_confidence_is_counted_instead_of_treated_as_unknown():
    from app.tagging import confidence_for_labels

    result = confidence_for_labels({
        "footpath": score("present", 1.0),
        "features": [{"tags": {"surface": score("concrete", 0.0)}}],
    })
    assert result == {"mean": 0.5, "minimum": 0.0, "known": 2, "unknown": 0}


def test_aggregate_weights_images_equally_and_keeps_unknown_image_count():
    from app.confidence import aggregate_confidence

    result = aggregate_confidence([
        {"mean": 0.8, "minimum": 0.8, "known": 5, "unknown": 0},
        {"mean": 0.4, "minimum": 0.4, "known": 1, "unknown": 0},
        {"mean": None, "minimum": None, "known": 0, "unknown": 1},
    ])
    assert result["mean"] == pytest.approx(0.6)
    assert result["minimum"] == 0.4
    assert result["known"] == 6 and result["unknown"] == 1 and result["images"] == 3


def test_empty_aggregate_is_unscored_not_perfect_confidence():
    from app.confidence import aggregate_confidence

    assert aggregate_confidence([]) == {"mean": None, "minimum": None, "known": 0, "unknown": 0, "images": 0}
