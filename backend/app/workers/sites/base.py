from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from app.capture.verification import PageState


class SiteAdapter(ABC):
    """Observation + navigation for one imagery website."""

    name: str
    data_class: str  # synthetic | research
    source: str

    @abstractmethod
    def view_url(self, lat: float, lng: float, heading: float, pitch: float, fov: float, pano_id: str | None = None, fault: str | None = None) -> str: ...

    @abstractmethod
    def allowed_request_hosts(self) -> list[str]:
        """Hosts the browser may contact at all (enforced by request routing)."""

    @abstractmethod
    def allowed_top_level_hosts(self) -> list[str]:
        """Hosts allowed for top-level navigation (agents cannot leave these)."""

    @abstractmethod
    async def read_state(self, page) -> PageState: ...

    async def wait_settled(self, page, timeout_s: float = 20.0, poll_s: float = 0.3) -> PageState:
        """Poll until the page leaves the loading state (or timeout)."""
        deadline = time.monotonic() + timeout_s
        st = await self.read_state(page)
        while time.monotonic() < deadline:
            if st.mode != "loading" and (st.mode != "street_view" or st.loaded or st.signals.get("obstructed")):
                return st
            await asyncio.sleep(poll_s)
            st = await self.read_state(page)
        st.signals["timeout"] = True
        return st


def host_matches(host: str, patterns: list[str]) -> bool:
    host = (host or "").lower()
    for p in patterns:
        p = p.lower()
        if p.startswith("*."):
            if host == p[2:] or host.endswith(p[1:]):
                return True
        elif host == p:
            return True
    return False


def url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
