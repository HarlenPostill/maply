from __future__ import annotations

from urllib.parse import urlencode, urlparse

from app.capture.verification import PageState, parse_capture_date_label
from app.geo.streetview_url import parse_streetview_url
from app.workers.sites.base import SiteAdapter

_STATE_JS = """() => {
  const r = document.getElementById('sv-root');
  if (!r) return {missing: true, title: document.title};
  const attr = document.getElementById('attr');
  const card = document.getElementById('card');
  return {
    state: r.dataset.state || '', mode: r.dataset.mode || '', overlay: r.dataset.overlay === '1',
    pano: r.dataset.panoId || '', captureLabel: r.dataset.captureLabel || '',
    cardText: card ? card.innerText : '',
    attribution: attr && attr.style.display !== 'none' ? attr.innerText : '',
    canvas: (() => { const c = document.querySelector('canvas'); return c ? [c.width, c.height] : null; })(),
    title: document.title,
  };
}"""


class MockPanoramaSite(SiteAdapter):
    name = "mock_panorama"
    data_class = "synthetic"
    source = "synthetic"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._host = urlparse(self.base_url).hostname or "127.0.0.1"

    def view_url(self, lat, lng, heading, pitch, fov, pano_id=None, fault=None) -> str:
        q = {"heading": f"{heading:.2f}", "pitch": f"{pitch:.2f}", "fov": f"{fov:.1f}"}
        if pano_id:
            q["pano"] = pano_id
        else:
            q["viewpoint"] = f"{lat:.7f},{lng:.7f}"
        if fault:
            q["fault"] = fault
        return f"{self.base_url}/mock/streetview?{urlencode(q)}"

    def allowed_request_hosts(self) -> list[str]:
        return [self._host]

    def allowed_top_level_hosts(self) -> list[str]:
        return [self._host]

    async def read_state(self, page) -> PageState:
        url = page.url
        try:
            d = await page.evaluate(_STATE_JS)
        except Exception as e:  # noqa: BLE001 - navigation in progress
            return PageState(mode="loading", url=url, message=f"evaluate failed: {e}")
        st = PageState(url=url, signals={"dom": d})
        if d.get("missing"):
            st.mode = "unknown"
            return st
        state = d.get("state")
        if state == "captcha":
            st.mode, st.message = "captcha", "Mock unusual-traffic interstitial requires an operator"
            return st
        if state == "no-imagery":
            st.mode, st.message = "no_imagery", "Site reports no imagery near this location"
            return st
        if state == "error":
            st.mode, st.message = "error", "Error frame displayed"
            return st
        if state == "loading":
            st.mode = "loading"
            return st
        # ready: read camera state from the address bar (same parser as live site)
        parsed = parse_streetview_url(url)
        st.mode = "street_view" if parsed.is_street_view else "unknown"
        st.loaded = state == "ready" and not d.get("overlay")
        if d.get("overlay"):
            st.message = "imagery obscured by an overlay panel"
            st.signals["obstructed"] = True
        if parsed.lat is not None:
            st.camera_lat, st.camera_lng, st.camera_provenance = parsed.lat, parsed.lng, "page_url"
        if parsed.heading is not None:
            st.heading, st.heading_provenance = parsed.heading, "page_url"
        if parsed.pitch is not None:
            st.pitch, st.pitch_provenance = parsed.pitch, "page_url"
        if parsed.fov is not None:
            st.fov, st.fov_provenance = parsed.fov, "page_url"
        if parsed.pano_id:
            st.pano_id, st.pano_id_provenance = parsed.pano_id, "page_url"
        label = d.get("captureLabel") or d.get("cardText") or ""
        date, precision = parse_capture_date_label(label)
        st.imagery_date, st.imagery_date_precision = date, precision
        st.imagery_date_provenance = "ui_text" if date else "unknown"
        st.imagery_date_raw = label or None
        st.attribution = (d.get("attribution") or "").strip() or None
        st.signals["canvas"] = d.get("canvas")
        return st
