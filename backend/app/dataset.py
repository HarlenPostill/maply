"""Dataset export: every saved photo with the metadata an image model needs.

Writes, inside the job folder:
  dataset/manifest.jsonl   one JSON object per photo (paths relative to the job folder)
  dataset/schema.json      what each field means
  dataset/README.md        how to use it

Streams from the database, so it works for 100k-photo jobs. Labels contain
versioned accessibility observations when the current image has been analysed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Job, Segment, Shot
from app.tag_schema import LabelResult, SCHEMA_VERSION
from app.tagging import labels_for, task_dict

VIEW_WORDS = {
    "perpendicular": "straight across",
    "oblique_forward": "angled ahead along the footpath",
    "oblique_backward": "angled back along the footpath",
    "approach_ahead": "into the intersection",
    "approach_ahead_left": "at the left-hand corner",
    "approach_ahead_right": "at the right-hand corner",
}

SCHEMA = {
    "format": "maply-dataset/2",
    "one_record_per": "saved Street View photo",
    "fields": {
        "id": "Stable photo id: <job>/<shot code>",
        "image": "Photo path relative to the job folder (JPEG or PNG as saved)",
        "thumb": "480 px preview path",
        "sha256 / width / height / bytes": "File checksum and size",
        "captured_at": "When the photo was saved (UTC). Not the imagery date.",
        "capture_source": "extension (you, in Chrome) or codex",
        "street.name / highway": "OpenStreetMap street name and road class",
        "street.osm_way_ids / osm_tags": "OSM ways and tags such as sidewalk, kerb, surface, maxspeed, lanes, oneway, lit",
        "street.segment_key / segment_bearing_deg": "maply segment and its direction (true north); left/right are relative to it",
        "footpath.side": "Compass side of the street photographed (north, east, ...); empty for intersection views",
        "footpath.relative_side": "left / right of the segment direction, or none",
        "view.kind / view.description": "street (along a block) or corner (approaching an intersection), and the view type",
        "planned": "Where maply asked for the photo: lat, lng, heading, pitch, fov and the Street View link",
        "camera": "Where Street View's camera actually was, read from the address bar when the photo was saved. This is the camera position, never the location of anything in the photo.",
        "near_intersection_m": "Distance from the photo point to the nearest intersection or corner (useful for kerb ramps)",
        "imagery_date": "Month the imagery was captured, from on-screen text; null when not shown",
        "quality.status / flags": "maply's checks: e.g. moved, heading_off, wrong_side, no_image_date",
        "labels": "Accessibility observations for the current image, including per-tag confidence, evidence, image regions, review reasons and provenance. Null until analysed. Confidence is model-reported, not calibrated accuracy.",
        "tagging": "Queue state, model, attempts and review status. Completed means analysed, not verified accessible.",
    },
    "label_schema_version": SCHEMA_VERSION,
    "label_schema": LabelResult.model_json_schema(),
    "label_provenance": "Server-added provenance contains image SHA256, model/provider, tagged_at, imagery_date, shot_id, segment_key, side and confidence_kind.",
}


def export(s: Session, job: Job) -> dict:
    settings = get_settings()
    root = settings.jobs_dir / job.slug
    out = root / "dataset"
    out.mkdir(parents=True, exist_ok=True)
    segs = {g.key: g for g in s.execute(select(Segment).where(Segment.job_id == job.id)).scalars()}
    n = 0
    tmp = out / "manifest.jsonl.part"
    with open(tmp, "w") as f:
        q = (
            select(Shot)
            .where(Shot.job_id == job.id, Shot.status.in_(("captured", "needs_check", "accepted", "retake")))
            .order_by(Shot.ordinal)
            .execution_options(yield_per=2000)
        )
        for sh in s.execute(q).scalars():
            r = sh.result or {}
            if not r.get("file"):
                continue
            g = segs.get(sh.segment_key)
            osm = g.osm if g else {}
            rec = {
                "id": f"{job.slug}/{sh.code}",
                "image": r["file"],
                "thumb": r.get("thumb"),
                "sha256": r.get("sha256"),
                "width": r.get("width"),
                "height": r.get("height"),
                "bytes": r.get("bytes"),
                "captured_at": sh.captured_at.isoformat() if sh.captured_at else (datetime.fromtimestamp(r["saved_at"], timezone.utc).isoformat() if r.get("saved_at") else None),
                "capture_source": sh.capture_source,
                "street": {
                    "name": sh.street,
                    "highway": osm.get("highway"),
                    "osm_way_ids": osm.get("osm_way_ids", []),
                    "osm_tags": osm.get("osm_tags", {}),
                    "segment_key": sh.segment_key,
                    "segment_bearing_deg": round(g.bearing_deg, 2) if g else None,
                },
                "footpath": {"side": sh.footpath or None, "relative_side": sh.side},
                "view": {"kind": sh.kind, "type": sh.view_kind, "description": VIEW_WORDS.get(sh.view_kind, sh.view_kind)},
                "planned": {"lat": sh.lat, "lng": sh.lng, "heading": sh.heading, "pitch": sh.pitch, "fov": sh.fov, "url": sh.url},
                "camera": {
                    "lat": r.get("camera_lat"),
                    "lng": r.get("camera_lng"),
                    "heading": r.get("camera_heading"),
                    "pitch": r.get("camera_pitch"),
                    "fov": r.get("camera_fov"),
                    "pano_id": r.get("pano_id"),
                    "offset_from_plan_m": r.get("camera_offset_m"),
                    "heading_delta_deg": r.get("heading_delta"),
                    "source_url": r.get("final_url"),
                },
                "near_intersection_m": sh.near_node_m,
                "imagery_date": {"value": r.get("image_date_iso"), "shown_as": r.get("image_date")},
                "quality": {"status": sh.status, "flags": list(sh.flags or []), "reviewed": sh.status in ("accepted", "retake")},
                "labels": labels_for(sh),
                "tagging": task_dict(sh),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(out / "manifest.jsonl")
    (out / "schema.json").write_text(json.dumps(SCHEMA, indent=2))
    (out / "README.md").write_text(
        f"# {job.name}: photo dataset\n\n"
        f"{n} photos. Exported {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.\n\n"
        "* `manifest.jsonl`: one JSON object per photo. Paths are relative to the job folder.\n"
        "* `schema.json`: what each field means, plus suggested label fields for phase 2.\n\n"
        "Street data © OpenStreetMap contributors (ODbL). Street View imagery © Google: keep it local and use it only for this research.\n"
    )
    return {"photos": n, "manifest": f"{settings.host_jobs_dir}/{job.slug}/dataset/manifest.jsonl"}
