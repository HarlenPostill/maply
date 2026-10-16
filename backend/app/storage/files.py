"""Local artefact storage.

* Writes are atomic: temp file in DATA_DIR/tmp -> fsync -> os.replace -> fsync dir.
* All paths stored in the database are relative to DATA_DIR, so a dataset
  directory can be moved or packaged without rewriting records.
* Files that are not referenced by any observation (e.g. a worker crashed
  between writing and committing) are moved to DATA_DIR/orphans, never
  silently counted as captures.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.config import get_settings


class StorageError(RuntimeError):
    pass


def data_root() -> Path:
    return get_settings().data_root


def rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(data_root()))


def resolve_rel(relpath: str) -> Path:
    """Resolve a DATA_DIR-relative path, refusing anything outside DATA_DIR."""
    root = data_root()
    p = (root / relpath).resolve()
    if root != p and root not in p.parents:
        raise StorageError("path escapes data directory")
    return p


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(final_path: Path, data: bytes) -> tuple[str, int]:
    settings = get_settings()
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.tmp_dir / f"{uuid.uuid4().hex}.part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final_path)  # atomic on the same filesystem
    _fsync_dir(final_path.parent)
    return sha256_bytes(data), len(data)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def job_dir(project_id, job_id) -> Path:
    return get_settings().projects_dir / str(project_id) / "jobs" / str(job_id)


def image_path(project_id, job_id, observation_id, fmt: str = "png") -> Path:
    return job_dir(project_id, job_id) / "images" / f"{observation_id}.{fmt}"


def thumb_path(project_id, job_id, observation_id) -> Path:
    return job_dir(project_id, job_id) / "thumbs" / f"{observation_id}.jpg"


def crop_path(project_id, job_id, crop_id) -> Path:
    return job_dir(project_id, job_id) / "crops" / f"{crop_id}.png"


def preview_path(project_id, job_id, worker_id: str) -> Path:
    return job_dir(project_id, job_id) / "workers" / f"{worker_id}-latest.jpg"


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


@dataclass
class ImageInfo:
    format: str
    width: int
    height: int


def image_info(data: bytes) -> ImageInfo:
    with Image.open(io.BytesIO(data)) as im:
        return ImageInfo(format=(im.format or "").lower(), width=im.width, height=im.height)


def make_thumbnail_bytes(data: bytes, max_side: int = 360) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=80)
        return out.getvalue()


def make_preview_bytes(data: bytes, max_side: int = 960) -> bytes:
    return make_thumbnail_bytes(data, max_side=max_side)


def crop_bytes(data: bytes, x: int, y: int, w: int, h: int) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        if x + w > im.width or y + h > im.height:
            raise StorageError("crop exceeds image bounds")
        out = io.BytesIO()
        im.crop((x, y, x + w, y + h)).save(out, format="PNG")
        return out.getvalue()


def verify_artifact(relpath: str, expected_sha: str, expected_w: int, expected_h: int, expected_fmt: str) -> list[str]:
    """Supervisor-side check of a stored artefact. Returns a list of problems."""
    problems: list[str] = []
    try:
        p = resolve_rel(relpath)
    except StorageError as e:
        return [str(e)]
    if not p.is_file():
        return ["artefact file missing"]
    data = p.read_bytes()
    if sha256_bytes(data) != expected_sha:
        problems.append("checksum mismatch")
    try:
        info = image_info(data)
    except Exception as e:  # noqa: BLE001
        return problems + [f"unreadable image: {e}"]
    if (info.width, info.height) != (expected_w, expected_h):
        problems.append(f"dimension mismatch {info.width}x{info.height} != {expected_w}x{expected_h}")
    if info.format != expected_fmt:
        problems.append(f"format mismatch {info.format} != {expected_fmt}")
    return problems


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def reconcile_orphans(referenced: set[str], min_age_s: float = 120.0) -> list[str]:
    """Move image files not referenced by any observation to DATA_DIR/orphans.

    `referenced` is the set of DATA_DIR-relative paths known to the database.
    Files younger than `min_age_s` are skipped because a worker may be between
    its atomic write and its completion transaction.
    """
    settings = get_settings()
    moved: list[str] = []
    now = time.time()
    root = settings.projects_dir
    if not root.exists():
        return moved
    for sub in ("images", "thumbs", "crops"):
        for p in root.glob(f"*/jobs/*/{sub}/*"):
            if not p.is_file():
                continue
            r = rel(p)
            if r in referenced or now - p.stat().st_mtime < min_age_s:
                continue
            dest = settings.orphans_dir / r
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            moved.append(r)
    for p in settings.tmp_dir.glob("*.part"):
        if now - p.stat().st_mtime > 3600:
            p.unlink(missing_ok=True)
    return moved


def dir_size_bytes(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total
