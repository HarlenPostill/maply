"""Check saved images and historical capture logs against the plan.

Reads the job folder:
  images/<code>_....png       one screenshot per shot
  results/*.jsonl             one JSON line per historical capture (latest wins)

For each shot it records the saved file, then uses the logged address-bar URL
(which Google rewrites to the camera's real position and heading) to check the
shot landed where it was planned. Problems become flags; nothing is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import re
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


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_image_date(text: str | None) -> str | None:
    """'Image capture: Mar 2023' / 'Mar 2023' -> '2023-03'. Copyright years are not dates."""
    if not text:
        return None
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{4})\b", text)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1)[:3].lower()]:02d}"
    m = re.search(r"\b(\d{4})-(\d{2})\b", text)
    return f"{m.group(1)}-{m.group(2)}" if m else None


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
    # tolerate another extension or suffix after the code, in the street folder or at the top level
    for d in dict.fromkeys((exact.parent, images_dir)):
        if d.is_dir():
            for p in sorted(d.glob(f"{code}*")):
                if p.suffix.lower() in (".png", ".jpg", ".jpeg") and (p.stem == code or p.stem.startswith(code + "_")):
                    return p
    return None


def thumb_for(root: Path, image: Path) -> Path:
    return root / "thumbs" / image.relative_to(root / "images").with_suffix(".jpg")


def make_thumb(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((480, 480))
        im.save(dst, format="JPEG", quality=82)


def camera_check(shot, final_url: str, image_date: str | None, segment_bearing: float | None) -> tuple[list[str], dict]:
    """Compare the view that was actually saved (its address-bar URL) with the plan."""
    flags: list[str] = []
    result: dict = {}
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
            result.update({"camera_heading": round(p.heading % 360, 1), "heading_delta": round(dh, 1)})
            if abs(dh) > HEADING_OFF_DEG:
                flags.append("heading_off")
            if segment_bearing is not None and shot.side in ("left", "right"):
                observed = side_of_heading(p.heading, segment_bearing)
                if observed not in (shot.side, "none"):
                    flags.append("wrong_side")
        if p.pitch is not None:
            result["camera_pitch"] = round(p.pitch, 1)
        if p.fov is not None:
            result["camera_fov"] = round(p.fov, 1)
        if p.pano_id:
            result["pano_id"] = p.pano_id
    if not image_date:
        flags.append("no_image_date")
    result["final_url"] = final_url or None
    result["image_date"] = image_date or None
    result["image_date_iso"] = parse_image_date(image_date)
    return flags, result


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
    st = image.stat()
    prev = shot.result or {}
    if prev.get("file") == rel and prev.get("bytes") == st.st_size and prev.get("saved_at") == st.st_mtime and not log:
        return shot.status, list(shot.flags or []), prev  # unchanged since last time: no re-reading 60k images
    data = image.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h, fmt = im.width, im.height, (im.format or "").lower()
    except Exception:  # noqa: BLE001
        return "needs_check", ["unreadable_image"], {**result, "file": rel}
    thumb = thumb_for(root, image)
    if not thumb.exists() or thumb.stat().st_mtime < image.stat().st_mtime:
        make_thumb(image, thumb)
    result.update({"file": rel, "thumb": str(thumb.relative_to(root)), "sha256": sha, "width": w, "height": h, "format": fmt, "bytes": st.st_size, "saved_at": st.st_mtime})
    if w < MIN_WIDTH:
        flags.append("small_image")
    if image.name != Path(shot.filename).name:
        flags.append("renamed_file")

    if not log:
        flags.append("not_logged")
    cflags, cres = camera_check(shot, (log or {}).get("final_url") or "", (log or {}).get("image_date"), segment_bearing)
    flags += cflags
    result.update(cres)

    # Keep an operator decision unless the file has changed since.
    if shot.status in ("accepted", "retake") and prev.get("sha256") == sha:
        return shot.status, flags, result
    return ("needs_check" if SERIOUS & set(flags) else "captured"), flags, result
