"""Contract tests for model output at the trust boundary."""

import copy

import pytest
from pydantic import ValidationError

from app.tag_schema import LabelResult, SCHEMA_VERSION, TAG_VALUES, build_prompt, annotation_schema, review_reasons


def observation(value="present", confidence=0.95, visibility="clear"):
    return {"value": value, "confidence": confidence, "evidence": "Visible in the foreground", "visibility": visibility}


def sample():
    return {
        "schema_version": SCHEMA_VERSION,
        "footpath": observation(),
        "features": [{
            "id": "f1", "kind": "footpath_section", "region": [0.1, 0.2, 0.9, 0.8],
            "location_description": "Near-side concrete strip behind the verge",
            "tags": {
                "presence": observation(), "surface": observation("concrete"),
                "path_type": observation("dedicated_footpath"),
                "continuity": observation("continuous_visible_extent"),
            },
        }],
        "notes": [], "review_reasons": [],
    }


def test_valid_round_trip_and_json_schema():
    labels = sample()
    assert LabelResult.model_validate(labels).model_dump(mode="json") == labels
    assert LabelResult.model_json_schema()["additionalProperties"] is False
    assert review_reasons(labels) == []


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf"), True, "0.9", None])
def test_known_confidence_is_finite_numeric_probability(confidence):
    labels = sample()
    labels["footpath"]["confidence"] = confidence
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)


def test_unknown_confidence_and_absence_evidence():
    labels = sample()
    labels["features"] = []
    labels["footpath"] = observation("unknown", None, "occluded")
    assert LabelResult.model_validate(labels).footpath.confidence is None
    assert any("unknown" in reason for reason in review_reasons(labels))
    labels["footpath"]["confidence"] = 0.99
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)
    labels["footpath"] = observation("absent", 0.9, "partial")
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)
    labels["footpath"] = observation("absent")
    assert any("route exclusion" in reason for reason in review_reasons(labels))


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(extra="invented"),
    lambda x: x["footpath"].update(evidence="  "),
    lambda x: x["features"][0]["tags"].update(wheelchair_accessible=observation("yes")),
    lambda x: x["features"][0]["tags"]["surface"].update(value="nice pavement"),
    lambda x: x["features"][0]["tags"].pop("surface"),
    lambda x: x["features"][0].update(region=[0.9, 0.1, 0.2, 0.5]),
    lambda x: x["features"][0].update(region=[0.1, 0.1, 1.2, 0.5]),
    lambda x: x["features"].append(copy.deepcopy(x["features"][0])),
    lambda x: x["footpath"].update(value="absent"),
    lambda x: x.update(features=[]),
])
def test_rejects_malformed_or_unsubstantiated_results(mutation):
    labels = sample()
    mutation(labels)
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)


def test_partial_missing_path_is_separate_presence_only_section():
    labels = sample()
    missing = {
        "id": "f2", "kind": "footpath_section", "region": [0.7, 0.2, 0.9, 0.5],
        "location_description": "Missing section at the right edge",
        "tags": {"presence": observation("absent")},
    }
    labels["features"].append(missing)
    LabelResult.model_validate(labels)
    assert any("missing pedestrian infrastructure" in reason for reason in review_reasons(labels))
    missing["tags"]["surface"] = observation("grass")
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)


def test_driveway_gap_and_steep_crossfall_require_review_even_when_confident():
    labels = sample()
    labels["features"].append({
        "id": "d1", "kind": "driveway", "region": [0.1, 0.2, 0.3, 0.4],
        "location_description": "Driveway pedestrian strip",
        "tags": {
            "surface": observation("brick_pavers"),
            "footpath_treatment": observation("tilts_sideways"),
            "cross_slope": observation("steep"),
            "entry_join": observation("open_gap"),
            "exit_join": observation("unknown", None, "occluded"),
        },
    })
    reasons = review_reasons(labels)
    assert any("d1.entry_join: potential route barrier" in reason for reason in reasons)
    assert any("d1.cross_slope: potential route barrier" in reason for reason in reasons)
    assert any("d1.exit_join: unknown" in reason for reason in reasons)


def test_missing_ramp_requires_crossing_context_and_no_invented_geometry():
    labels = sample()
    ramp = {
        "id": "r1", "kind": "kerb_ramp", "region": [0.1, 0.2, 0.3, 0.4],
        "location_description": "Crossing approach",
        "tags": {
            "presence": observation("absent"),
            "crossing_context": observation("crossing_approach"),
            **{name: observation("unknown", None) for name in ["bottom_join", "slope", "cross_slope", "turning_space", "tactile_indicators"]},
        },
    }
    labels["features"].append(ramp)
    LabelResult.model_validate(labels)
    ramp["tags"]["crossing_context"] = observation("ordinary_roadside_kerb")
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)
    ramp["tags"]["crossing_context"] = observation("crossing_approach")
    ramp["tags"]["slope"] = observation("steep")
    with pytest.raises(ValidationError):
        LabelResult.model_validate(labels)


def test_prompt_carries_scope_vocabulary_and_image_trust_boundary():
    prompt = build_prompt({"street": "Beaconsfield St", "side": "left", "heading": 207})
    for text in ["Beaconsfield St", "left", "207", "brick_pavers", "open_gap", "confidence=null", "FULL SOURCE IMAGE", "untrusted context", "Camera latitude/longitude", "Road/OSM surface"]:
        assert text in prompt


def test_verge_hazard_is_not_reported_as_a_route_barrier():
    labels = sample()
    labels["features"].append({
        "id": "h1", "kind": "hazard", "region": [0.1, 0.2, 0.3, 0.4],
        "location_description": "Raised root contained within the grass verge",
        "tags": {
            "type": observation("raised_root"),
            "route_relation": observation("beside_route"),
            "affected_width": observation("outside_route"),
            "bypass": observation("visible"),
            "persistence": observation("fixed"),
        },
    })
    assert review_reasons(labels) == []


def test_generation_schema_has_portable_structure_and_complete_enum_vocabulary():
    schema = annotation_schema()

    def inspect(node):
        if isinstance(node, dict):
            assert not {"$defs", "$ref", "prefixItems", "const"} & node.keys()
            assert not isinstance(node.get("additionalProperties"), dict)
            for value in node.values():
                inspect(value)
        elif isinstance(node, list):
            for value in node:
                inspect(value)

    inspect(schema)
    feature = schema["properties"]["features"]["items"]
    assert feature["properties"]["region"]["anyOf"][0]["items"]["type"] == "number"
    tags = feature["properties"]["tags"]["properties"]
    for kind, allowed in TAG_VALUES.items():
        assert kind in feature["properties"]["kind"]["enum"]
        for name, values in allowed.items():
            assert set(values) <= set(tags[name]["properties"]["value"]["enum"])
    assert schema["properties"]["footpath"]["properties"]["value"]["enum"] == ["present", "absent", "unknown"]
    assert schema["properties"]["footpath"]["properties"]["confidence"]["anyOf"][1] == {"type": "null"}
