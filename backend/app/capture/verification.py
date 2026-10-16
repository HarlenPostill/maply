"""Deterministic capture verification.

Two layers:

1. Frame checks (`check_frame`): is this a loaded, stable Street View frame,
   rather than a blank, error or transition frame?
2. Policy checks (`evaluate_capture`): does what the page reports (camera
   position, orientation, imagery date, attribution) match the planned task
   well enough to accept, and how certain is the association between the
   image and the intended footpath?

Nothing here trusts an agent's claim of success. All inputs are observed page
state plus the pixels that were actually written to disk.
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
from PIL import Image

from app.geo.projection import geodesic_distance, heading_delta, side_of_heading

# --------------------------------------------------------------------------
# Page state reported by site adapters
# --------------------------------------------------------------------------

PAGE_MODES = (
    "street_view",
    "map",
    "loading",
    "error",
    "consent",
    "captcha",
    "login",
    "no_imagery",
    "blocked",
    "unknown",
)
MANUAL_MODES = ("consent", "captcha", "login")


@dataclass
class PageState:
    mode: str = "unknown"
    url: str = ""
    loaded: bool = False
    camera_lat: float | None = None
    camera_lng: float | None = None
    camera_provenance: str = "unknown"
    heading: float | None = None
    heading_provenance: str = "unknown"
    pitch: float | None = None
    pitch_provenance: str = "unknown"
    fov: float | None = None
    fov_provenance: str = "unknown"
    pano_id: str | None = None
    pano_id_provenance: str = "unknown"
    imagery_date: str | None = None  # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    imagery_date_precision: str = "unknown"  # year|month|day|unknown
    imagery_date_provenance: str = "unknown"
    imagery_date_raw: str | None = None
    attribution: str | None = None
    message: str | None = None
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_capture_date_label(text: str | None) -> tuple[str | None, str]:
    """Parse an *explicit imagery capture date label* such as "Image capture: Mar 2023".

    Copyright years ("(c) 2024 Google") are deliberately NOT accepted as dates.
    Returns (iso_date_or_None, precision).
    """
    if not text:
        return None, "unknown"
    m = re.search(r"(?:image\s+capture|captured)\s*:?\s*([A-Za-z]{3,9})\.?\s+(\d{4})", text, re.I)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if mon:
            return f"{int(m.group(2)):04d}-{mon:02d}", "month"
    m = re.search(r"(?:image\s+capture|captured)\s*:?\s*(\d{4})-(\d{2})(?:-(\d{2}))?", text, re.I)
    if m:
        if m.group(3):
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "day"
        return f"{m.group(1)}-{m.group(2)}", "month"
    m = re.search(r"(?:image\s+capture|captured)\s*:?\s*(\d{4})\b", text, re.I)
    if m:
        return m.group(1), "year"
    return None, "unknown"


def imagery_age_months(imagery_date: str | None, precision: str, today: date | None = None) -> float | None:
    if not imagery_date or precision == "unknown":
        return None
    today = today or date.today()
    parts = imagery_date.split("-")
    y = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 6  # mid-year for year precision (documented)
    return (today.year - y) * 12 + (today.month - m)


# --------------------------------------------------------------------------
# Frame checks
# --------------------------------------------------------------------------


@dataclass
class FrameStats:
    width: int
    height: int
    mean_luma: float
    std_luma: float
    dominant_fraction: float
    edge_density: float

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}


def _luma(data: bytes, max_side: int = 320) -> tuple[np.ndarray, int, int]:
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.width, im.height
        g = im.convert("L")
        g.thumbnail((max_side, max_side))
        return np.asarray(g, dtype=np.float32), w, h


def frame_stats(data: bytes) -> FrameStats:
    a, w, h = _luma(data)
    q = (a // 8).astype(np.int32)
    counts = np.bincount(q.ravel(), minlength=32)
    dominant = float(counts.max() / max(1, q.size))
    gx = np.abs(np.diff(a, axis=1))
    gy = np.abs(np.diff(a, axis=0))
    edges = float(((gx > 20).mean() + (gy > 20).mean()) / 2.0)
    return FrameStats(w, h, float(a.mean()), float(a.std()), dominant, edges)


def frame_difference(a: bytes, b: bytes) -> float:
    """Mean absolute luminance difference (0-255) between two frames."""
    la, _, _ = _luma(a)
    lb, _, _ = _luma(b)
    if la.shape != lb.shape:
        return 255.0
    return float(np.abs(la - lb).mean())


@dataclass
class FrameCheck:
    ok: bool
    flags: list[str]
    stats: dict
    stability_diff: float | None


def _central_stats(data: bytes) -> tuple[float, float]:
    """Std-dev and dominant-bin fraction of the central 60% of the frame.

    Site chrome (cards, attribution, controls) sits near the edges and can make a
    blank panorama look 'detailed'; the centre is where imagery must be.
    """
    a, _, _ = _luma(data)
    h, w = a.shape
    c = a[int(h * 0.2) : int(h * 0.8), int(w * 0.2) : int(w * 0.8)]
    q = (c // 8).astype(np.int32)
    counts = np.bincount(q.ravel(), minlength=32)
    return float(c.std()), float(counts.max() / max(1, q.size))


def check_frame(data: bytes, second: bytes | None = None, *, max_stability_diff: float = 2.5) -> FrameCheck:
    st = frame_stats(data)
    c_std, c_dom = _central_stats(data)
    flags: list[str] = []
    if st.std_luma < 6.0 or c_std < 4.0:
        flags.append("blank_frame")
    if st.dominant_fraction > 0.92 or c_dom > 0.97:
        flags.append("uniform_frame")
    if st.edge_density < 0.004:
        flags.append("low_detail_frame")
    diff = None
    if second is not None:
        diff = frame_difference(data, second)
        if diff > max_stability_diff:
            flags.append("unstable_transition_frame")
    stats = st.to_dict() | {"central_std_luma": round(c_std, 4), "central_dominant_fraction": round(c_dom, 4)}
    return FrameCheck(ok=not flags, flags=flags, stats=stats, stability_diff=None if diff is None else round(diff, 3))


# --------------------------------------------------------------------------
# Policy evaluation
# --------------------------------------------------------------------------


@dataclass
class CapturePolicy:
    max_camera_offset_m: float = 20.0
    no_imagery_distance_m: float = 60.0
    heading_tolerance_deg: float = 10.0
    max_imagery_age_months: int | None = 60
    unknown_date_policy: str = "needs_review"  # needs_review | accept_flagged | reject
    require_attribution: bool = True

    @classmethod
    def from_params(cls, p: dict) -> CapturePolicy:
        return cls(
            max_camera_offset_m=float(p.get("max_camera_offset_m", 20.0)),
            no_imagery_distance_m=float(p.get("no_imagery_distance_m", 60.0)),
            heading_tolerance_deg=float(p.get("heading_tolerance_deg", 10.0)),
            max_imagery_age_months=p.get("max_imagery_age_months", 60),
            unknown_date_policy=p.get("unknown_date_policy", "needs_review"),
        )


@dataclass
class PlannedViewInfo:
    id: str
    side: str
    view_kind: str
    heading: float
    pitch: float
    fov: float
    viewport_w: int
    viewport_h: int


@dataclass
class CaptureEvaluation:
    verification_status: str  # verified | needs_review | rejected
    association_status: str  # geometry_consistent | uncertain | unresolved
    flags: list[str]
    reject_reason: str | None
    camera_offset_m: float | None
    heading_delta_deg: float | None
    evidence: dict


def evaluate_capture(
    *,
    state: PageState,
    view: PlannedViewInfo,
    requested_lat: float,
    requested_lng: float,
    segment_bearing: float,
    frame: FrameCheck,
    policy: CapturePolicy,
    in_scope: bool | None,
    today: date | None = None,
) -> CaptureEvaluation:
    flags: list[str] = list(frame.flags)
    review = False
    reject: str | None = None

    if state.mode != "street_view" or not state.loaded:
        reject = f"page not in a loaded Street View state (mode={state.mode})"
    if not frame.ok:
        reject = reject or "frame failed quality checks: " + ",".join(frame.flags)

    # Location
    offset = None
    if state.camera_lat is not None and state.camera_lng is not None:
        offset = geodesic_distance(requested_lng, requested_lat, state.camera_lng, state.camera_lat)
        if offset > policy.no_imagery_distance_m:
            flags.append("camera_beyond_no_imagery_distance")
            reject = reject or f"camera {offset:.0f} m from the requested sample"
        elif offset > policy.max_camera_offset_m:
            flags.append("camera_far_from_sample")
            review = True
    else:
        flags.append("camera_position_unknown")
        review = True

    if in_scope is False:
        flags.append("camera_outside_scope")
        reject = reject or "camera position outside the approved geographic scope"
    elif in_scope is None:
        flags.append("scope_unverified")
        review = True

    # Orientation
    hd = None
    if state.heading is not None:
        hd = heading_delta(state.heading, view.heading)
        if abs(hd) > policy.heading_tolerance_deg:
            flags.append("heading_mismatch")
            review = True
        observed_side = side_of_heading(state.heading, segment_bearing)
        if view.side in ("left", "right") and observed_side not in (view.side, "none"):
            flags.append("observed_heading_on_other_side")
            review = True
    else:
        flags.append("heading_unverified")
        review = True
    if state.pitch is None:
        flags.append("pitch_unverified")
    if state.fov is None:
        flags.append("fov_unverified")

    # Imagery date: only explicit capture-date evidence counts.
    if state.imagery_date is None or state.imagery_date_precision == "unknown":
        flags.append("imagery_date_unknown")
        if policy.unknown_date_policy == "needs_review":
            review = True
        elif policy.unknown_date_policy == "reject":
            reject = reject or "imagery date unknown and policy is reject"
    elif policy.max_imagery_age_months is not None:
        age = imagery_age_months(state.imagery_date, state.imagery_date_precision, today)
        if age is not None and age > policy.max_imagery_age_months:
            flags.append("imagery_older_than_desired")
            review = True

    if policy.require_attribution and not state.attribution:
        flags.append("attribution_missing")
        review = True
    if not state.pano_id:
        flags.append("panorama_id_unknown")

    # Association between image and intended footpath. Geometry can at best be
    # *consistent*; visual confirmation needs an operator (or a later analysis step).
    if offset is None or state.heading is None:
        assoc = "unresolved"
    elif offset <= policy.max_camera_offset_m and abs(hd or 0) <= policy.heading_tolerance_deg and "observed_heading_on_other_side" not in flags:
        assoc = "geometry_consistent"
    else:
        assoc = "uncertain"

    status = "rejected" if reject else ("needs_review" if review else "verified")
    evidence = {
        "page_state": state.to_dict(),
        "frame": {"flags": frame.flags, "stats": frame.stats, "stability_diff": frame.stability_diff},
        "camera_offset_m": None if offset is None else round(offset, 2),
        "heading_delta_deg": None if hd is None else round(hd, 2),
        "segment_bearing_deg": round(segment_bearing, 3),
        "in_scope": in_scope,
        "policy": asdict(policy),
        "association_basis": "camera offset + observed heading vs planned (geometry only; footpath visibility not visually confirmed)",
    }
    return CaptureEvaluation(
        verification_status=status,
        association_status=assoc,
        flags=sorted(set(flags)),
        reject_reason=reject,
        camera_offset_m=None if offset is None else round(offset, 2),
        heading_delta_deg=None if hd is None else round(hd, 2),
        evidence=evidence,
    )
