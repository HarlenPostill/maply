"""Google Maps web client (visible browser interaction).

Navigation uses the documented Maps URLs scheme (`map_action=pano`). No tile
endpoints or private APIs are called by this application; the browser loads
the page the same way it would for a person.

Observation is best effort and versioned by provenance:
* camera position / heading / pitch / fov / panorama id: parsed from the
  address bar the client writes (`page_url`). Not a published contract.
* imagery date: ONLY from an explicit "Image capture: <Mon YYYY>" label in
  visible UI text (`ui_text`). Copyright years and URL parameters are never
  used. If absent, the date is `unknown`.
* attribution: visible "© <year> Google" style text, preserved verbatim.

This adapter needs validation against the live site by the operator (see
docs/LIVE_INTEGRATION.md); UI text and URL formats can change without notice.
"""

from __future__ import annotations

import re

from app.capture.verification import PageState, parse_capture_date_label
from app.geo.streetview_url import build_maps_pano_url, parse_streetview_url
from app.workers.sites.base import SiteAdapter, url_host

_STATE_JS = """() => {
  const text = (document.body && document.body.innerText || '').slice(0, 8000);
  const canvases = [...document.querySelectorAll('canvas')].map(c => {
    const r = c.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];
  });
  return {
    text,
    canvases,
    recaptcha: !!document.querySelector('iframe[src*="recaptcha"], #captcha-form, form[action*="sorry"]'),
    readyState: document.readyState,
    title: document.title,
  };
}"""

_ERROR_PATTERNS = [
    r"Oops! Something went wrong",
    r"can't load Google Maps correctly",
    r"This page didn.t load Google Maps correctly",
]
_ATTRIBUTION_RE = re.compile(r"(©\s*\d{4}\s*Google[^\n]{0,40}|Image capture:[^\n]{0,40}©[^\n]{0,30}Google)")


class GoogleMapsSite(SiteAdapter):
    name = "google_maps"
    data_class = "research"
    source = "google_maps"

    def view_url(self, lat, lng, heading, pitch, fov, pano_id=None, fault=None) -> str:
        return build_maps_pano_url(lat, lng, heading, pitch, fov, pano_id)

    def allowed_request_hosts(self) -> list[str]:
        return [
            "www.google.com",
            "google.com",
            "maps.google.com",
            "consent.google.com",
            "accounts.google.com",  # only so a login prompt can be *detected* and handed to an operator
            "*.gstatic.com",
            "*.googleapis.com",
            "*.ggpht.com",
            "*.googleusercontent.com",
            "www.google.com.au",
        ]

    def allowed_top_level_hosts(self) -> list[str]:
        return ["www.google.com", "google.com", "www.google.com.au", "maps.google.com", "consent.google.com", "accounts.google.com"]

    async def read_state(self, page) -> PageState:
        url = page.url
        host = url_host(url)
        st = PageState(url=url)
        if host.startswith("consent."):
            st.mode, st.message = "consent", "Google consent page: operator decision required"
            return st
        if host.startswith("accounts."):
            st.mode, st.message = "login", "Sign-in prompt: operator required (workers never sign in)"
            return st
        if "/sorry/" in url:
            st.mode, st.message = "captcha", "Unusual-traffic challenge: operator required (never bypassed)"
            return st
        try:
            d = await page.evaluate(_STATE_JS)
        except Exception as e:  # noqa: BLE001
            st.mode, st.message = "loading", f"evaluate failed: {e}"
            return st
        text = d.get("text") or ""
        st.signals = {"canvases": d.get("canvases"), "readyState": d.get("readyState"), "title": d.get("title")}
        if d.get("recaptcha") or re.search(r"unusual traffic", text, re.I):
            st.mode, st.message = "captcha", "Challenge detected: operator required"
            return st
        for pat in _ERROR_PATTERNS:
            if re.search(pat, text, re.I):
                st.mode, st.message = "error", pat
                return st
        parsed = parse_streetview_url(url)
        big_canvas = any(w >= 300 and h >= 200 for w, h in (d.get("canvases") or []))
        st.signals["parsed_url"] = parsed.to_dict()
        if parsed.url_form == "maps_urls_api":
            st.mode = "loading"  # client has not rewritten the URL yet
            return st
        if not parsed.is_street_view:
            st.mode = "map" if parsed.lat is not None else "unknown"
            st.message = "Client is showing a map, not Street View (likely no imagery near the requested point)"
            return st
        st.mode = "street_view"
        st.loaded = big_canvas and d.get("readyState") == "complete"
        st.camera_lat, st.camera_lng, st.camera_provenance = parsed.lat, parsed.lng, "page_url"
        if parsed.heading is not None:
            st.heading, st.heading_provenance = parsed.heading, "page_url"
        if parsed.pitch is not None:
            st.pitch, st.pitch_provenance = parsed.pitch, "page_url"
        if parsed.fov is not None:
            st.fov, st.fov_provenance = parsed.fov, "page_url"
        if parsed.pano_id:
            st.pano_id, st.pano_id_provenance = parsed.pano_id, "page_url"
            st.signals["pano_kind"] = parsed.pano_kind
        date, precision = parse_capture_date_label(text)
        st.imagery_date, st.imagery_date_precision = date, precision
        st.imagery_date_provenance = "ui_text" if date else "unknown"
        m = re.search(r"Image capture:[^\n]{0,30}", text)
        st.imagery_date_raw = m.group(0) if m else None
        am = _ATTRIBUTION_RE.search(text)
        st.attribution = am.group(0).strip() if am else None
        return st
