"""Local mock panorama site (synthetic). Disabled with ENABLE_MOCK_PANORAMA=false."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.geo.streetview_url import parse_streetview_url
from app.mock import panorama as mock

router = APIRouter(prefix="/mock", include_in_schema=False)

HEADERS = {"Content-Security-Policy": mock.csp_header(), "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "X-Synthetic": "maply-mock"}


def _html(body: str) -> HTMLResponse:
    return HTMLResponse(body, headers=HEADERS)


def _f(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@router.get("/streetview")
def streetview(request: Request):
    q = request.query_params
    heading, pitch, fov = _f(q.get("heading"), 0.0) % 360, _f(q.get("pitch"), 0.0), _f(q.get("fov"), 90.0)
    fault = q.get("fault")
    if fault == "captcha" and request.cookies.get("mock_cleared") != "1":
        clean = {k: v for k, v in q.items() if k != "fault"}
        return _html(mock.render_captcha("/mock/streetview?" + urlencode(clean)))
    if fault == "captcha":
        fault = None
    pano = None
    if q.get("forward_from"):
        base = mock.resolve_pano_id(q["forward_from"])
        pano = mock.neighbour_in_direction(base, heading) if base else None
        if pano is None and base is not None:
            pano = base
    elif q.get("pano"):
        pano = mock.resolve_pano_id(q["pano"])
    elif q.get("viewpoint"):
        try:
            lat, lng = (float(x) for x in q["viewpoint"].split(","))
        except ValueError:
            return _html(mock.render_no_imagery(0, 0))
        pano = mock.resolve_viewpoint(lat, lng)
        if pano is None:
            return _html(mock.render_no_imagery(lat, lng))
    if pano is None:
        return _html(mock.render_no_imagery(0, 0))
    if not fault:
        fault = mock.auto_fault(pano.pano_id, heading, get_settings().mock_fault_rate)
    return _html(mock.render_panorama(pano, heading, pitch, fov, fault))


@router.get("/streetview/{rest:path}")
def streetview_client_path(rest: str, request: Request):
    p = parse_streetview_url("/mock/streetview/" + rest)
    if p.pano_id and mock.resolve_pano_id(p.pano_id):
        return _html(mock.render_panorama(mock.resolve_pano_id(p.pano_id), (p.heading or 0) % 360, p.pitch or 0, p.fov or 90, None))
    return RedirectResponse("/mock/streetview?" + urlencode({"viewpoint": f"{p.lat},{p.lng}"}) if p.lat is not None else "/mock/streetview")


@router.get("/maps/{rest:path}")
def maps(rest: str):
    p = parse_streetview_url("/mock/maps/" + rest)
    return _html(mock.render_no_imagery(p.lat or 0, p.lng or 0))
