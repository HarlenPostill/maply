"""Google Maps Street View URL handling.

Building: uses the documented Maps URLs scheme
(https://developers.google.com/maps/documentation/urls/get-started#street-view-action):
``https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=LAT,LNG&heading=H&pitch=P&fov=F``.

Parsing: reads the camera values the web client writes into its address bar
(``@lat,lng,3a,{fov}y,{heading}h,{tilt}t/data=...!1s{pano}!2e{kind}``). That
format is not a published contract, so every parsed value is tagged with the
provenance ``page_url`` and treated as an observation that can be absent or
wrong. Chrome also emits numeric-a modes such as 15a/16a; these are treated as
Street View only when explicit panorama data is present. Imagery dates are never
derived from URL parameters.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

_AT_RE = re.compile(
    r"@(?P<lat>-?\d+(?:\.\d+)?),(?P<lng>-?\d+(?:\.\d+)?)"
    r"(?:,(?P<rest>[^/?]*))?"
)
_PANO_RE = re.compile(r"!1s(?P<pano>[^!]+)!2e(?P<kind>\d+)")


@dataclass
class ParsedStreetViewUrl:
    lat: float | None = None
    lng: float | None = None
    is_street_view: bool = False
    heading: float | None = None
    pitch: float | None = None
    fov: float | None = None
    pano_id: str | None = None
    # 0 = Google-collected panorama, 10 = user-contributed photosphere (as observed)
    pano_kind: str | None = None
    provenance: str = "page_url"
    url_form: str = "unknown"  # client_path | maps_urls_api | unknown

    def to_dict(self) -> dict:
        return asdict(self)


def parse_streetview_url(url: str) -> ParsedStreetViewUrl:
    out = ParsedStreetViewUrl()
    if not url:
        return out
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query)

    if qs.get("map_action", [""])[0] == "pano" or qs.get("api", [""])[0] == "1":
        out.url_form = "maps_urls_api"
        out.is_street_view = qs.get("map_action", [""])[0] == "pano"
        vp = qs.get("viewpoint", [""])[0]
        if vp and "," in vp:
            a, b = vp.split(",", 1)
            out.lat, out.lng = _f(a), _f(b)
        out.heading = _f(qs.get("heading", [None])[0])
        out.pitch = _f(qs.get("pitch", [None])[0])
        out.fov = _f(qs.get("fov", [None])[0])
        out.pano_id = qs.get("pano", [None])[0]
        out.provenance = "request_url"
        return out

    path = unquote(parsed.path)
    m = _AT_RE.search(path)
    if not m:
        return out
    out.url_form = "client_path"
    out.lat, out.lng = _f(m.group("lat")), _f(m.group("lng"))
    rest = m.group("rest") or ""
    tokens = [t for t in rest.split(",") if t]
    pm = _PANO_RE.search(path)
    # Chrome also emits 15a/16a (observed live in Sydney). Numeric-a alone
    # can describe an aerial map, so require the explicit panorama data too.
    has_pano_data = pm is not None and '!1e1!' in path
    mode = tokens[0] if tokens else ''
    out.is_street_view = mode == '3a' or bool(
        re.fullmatch(r'\d+(?:\.\d+)?a', mode) and has_pano_data
    )
    for t in tokens:
        if t == "3a":
            out.is_street_view = True
        elif t.endswith("y") and out.is_street_view:
            out.fov = _f(t[:-1])
        elif t.endswith("h"):
            out.heading = _f(t[:-1])
        elif t.endswith("t"):
            tilt = _f(t[:-1])
            # The client encodes tilt with 90 = horizon; pitch = tilt - 90.
            out.pitch = None if tilt is None else tilt - 90.0
    if pm:
        out.pano_id = pm.group("pano")
        out.pano_kind = pm.group("kind")
    return out


def build_maps_pano_url(
    lat: float,
    lng: float,
    heading: float,
    pitch: float,
    fov: float,
    pano_id: str | None = None,
) -> str:
    params = {
        "api": "1",
        "map_action": "pano",
        "viewpoint": f"{lat:.7f},{lng:.7f}",
        "heading": f"{_clamp(heading, -180, 360):.1f}",
        "pitch": f"{_clamp(pitch, -90, 90):.1f}",
        "fov": f"{_clamp(fov, 10, 100):.1f}",
    }
    if pano_id:
        params["pano"] = pano_id
    return "https://www.google.com/maps/@?" + urlencode(params, quote_via=quote, safe=",")


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
