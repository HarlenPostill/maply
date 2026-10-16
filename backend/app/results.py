"""Check what Codex saved against the plan.

Reads the job folder:
  images/<code>_....png       one screenshot per shot
  results/*.jsonl             one JSON line per shot Codex logged (latest wins)

For each shot it records the saved file, then uses the logged address-bar URL
(which Google rewrites to the camera's real position and heading) to check the
shot landed where it was planned. Problems become flags; nothing is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from PIL import Image

from app.geo.projection import geodesic_distance, heading_delta, side_of_heading
from app.geo.streetview_url import parse_streetview_url

MOVED_M = 15.0  # camera further than this from the planned point -> flag
FAR_M = 40.0  # ... and this far -> needs a check
HEADING_OFF_DEG = 25.0
MIN_WIDTH = 600

# Flags that mean "look at this one before trusting it".
SERIOUS = {"far_from_plan", "heading_off", "wrong_side", "not_street_view", "file_missing", "unreadable_image", "small_image"}


def read_logs(results_dir: Path) -> dict[str, dict]:
    """Latest log entry per shot code across all results files (oldest file first)."""
    entries: dict[str, dict] = {}
    if not results_dir.is_dir():
        return entries
    for p in sorted(results_dir.iterdir(), key=lambda q: q.stat().st_mtime):
        if p.suffix == ".jsonl":
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip().rstrip(",")
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("shot"):
                    entries[str(d["shot"]).strip()] = {**d, "_log": p.name}
        elif p.suffix == ".csv":
            for d in csv.DictReader(io.StringIO(p.read_text(errors="replace"))):
                if d.get("shot"):
                    entries[d["shot"].strip()] = {**d, "_log": p.name}
    return entries


def find_image(images_dir: Path, filename: str, code: str) -> Path | None:
    exact = images_dir / filename
    if exact.is_file():
        return exact
    if images_dir.is_dir():  # tolerate a different suffix after the code
        for p in sorted(images_dir.glob(f"{code}*")):
            if p.suffix.lower() in (".png", ".jpg", ".jpeg") and (p.stem == code or p.stem.startswith(code + "_")):
                return p
    return None


def make_thumb(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((480, 480))
        im.save(dst, format="JPEG", quality=82)


def evaluate(shot, root: Path, log: dict | None, segment_bearing: float | None) -> tuple[str, list[str], dict]:
    """Return (status, flags, result) for one shot. `shot` has code, filename, lat, lng, heading, side, status, result."""
    flags: list[str] = []
    result: dict = {}
    image = find_image(root / "images", shot.filename, shot.code)
    logged = (log or {}).get("status", "").strip().lower() if log else ""

    if log:
        result.update({k: log.get(k) for k in ("status", "final_url", "image_date", "notes", "_log")})
    if logged in ("no_imagery", "blocked") and image is None:
        return logged, [], result

    if image is None:
        if logged == "captured":
            return "needs_check", ["file_missing"], result
        return "pending", [], result

    rel = str(image.relative_to(root))
    data = image.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h, fmt = im.width, im.height, (im.format or "").lower()
    except Exception:  # noqa: BLE001
        return "needs_check", ["unreadable_image"], {**result, "file": rel}
    thumb = root / "thumbs" / (image.stem + ".jpg")
    if not thumb.exists() or thumb.stat().st_mtime < image.stat().st_mtime:
        make_thumb(image, thumb)
    result.update({"file": rel, "thumb": str(thumb.relative_to(root)), "sha256": sha, "width": w, "height": h, "format": fmt, "saved_at": image.stat().st_mtime})
    if w < MIN_WIDTH:
        flags.append("small_image")
    if image.name != shot.filename:
        flags.append("renamed_file")

    if not log:
        flags.append("not_logged")
    final_url = (log or {}).get("final_url") or ""
    if not final_url:
        flags.append("no_final_url")
    else:
        p = parse_streetview_url(final_url)
        if not p.is_street_view or p.url_form != "client_path":
            flags.append("not_street_view" if p.lat is not None and not p.is_street_view else "url_not_lined_up")
        if p.lat is not None:
            d = geodesic_distance(shot.lng, shot.lat, p.lng, p.lat)
            result.update({"camera_lat": p.lat, "camera_lng": p.lng, "camera_offset_m": round(d, 1)})
            if d > FAR_M:
                flags.append("far_from_plan")
            elif d > MOVED_M:
                flags.append("moved")
        if p.heading is not None:
            dh = heading_delta(p.heading, shot.heading)
            result.update({"camera_heading": round(p.heading, 1), "heading_delta": round(dh, 1)})
            if abs(dh) > HEADING_OFF_DEG:
                flags.append("heading_off")
            if segment_bearing is not None and shot.side in ("left", "right"):
                observed = side_of_heading(p.heading, segment_bearing)
                if observed not in (shot.side, "none"):
                    flags.append("wrong_side")
        if p.pitch is not None:
            result["camera_pitch"] = round(p.pitch, 1)
        if p.pano_id:
            result["pano_id"] = p.pano_id
    if not (log or {}).get("image_date"):
        flags.append("no_image_date")

    # Keep an operator decision unless the file has changed since.
    prev = shot.result or {}
    if shot.status in ("accepted", "retake") and prev.get("sha256") == sha:
        return shot.status, flags, result
    return ("needs_check" if SERIOUS & set(flags) else "captured"), flags, result
