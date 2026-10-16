"""Bounded live check of the Google Maps (web) deterministic path.

Creates ONE tiny plan (a single sample, two perpendicular views) and a job with
target google_maps, max 2 captures, 2 requests/minute, then waits for it and
prints the recorded provenance so you can judge the page adapter.

Runs only with --confirm-approved-scope, against a running local stack:

    cd backend
    uv run python ../scripts/live_check.py --confirm-approved-scope \
        --lat -33.87410 --lng 151.20680 --bearing-to-lat -33.87380 --bearing-to-lng 151.20685

It uses the normal API (http://127.0.0.1:8000) and the normal worker; the
project's limits, scope and approval expiry all apply. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--project", help="project id (default: first project)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--bearing-to-lat", type=float, required=True, help="a second point ~20-40 m along the street, defines segment direction")
    ap.add_argument("--bearing-to-lng", type=float, required=True)
    ap.add_argument("--confirm-approved-scope", action="store_true", help="you confirm this location and collection are within your approval")
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args()
    if not a.confirm_approved_scope:
        print("Refusing to run: pass --confirm-approved-scope to confirm this live collection is within your approval.")
        return 2
    c = httpx.Client(base_url=a.base, timeout=30)
    pid = a.project or c.get("/api/projects").json()[0]["id"]
    route = c.post(f"/api/projects/{pid}/routes", json={"name": "live check", "kind": "route", "geojson": {"type": "LineString", "coordinates": [[a.lng, a.lat], [a.bearing_to_lng, a.bearing_to_lat]]}})
    route.raise_for_status()
    plan = c.post(
        f"/api/projects/{pid}/plans",
        json={"name": "live check", "route_ids": [route.json()["id"]], "params": {"sampling_interval_m": 500, "views_per_side": 1, "include_crossing_approaches": False, "include_route_endpoints_as_nodes": False}},
    )
    if plan.status_code != 200:
        print("plan refused:", plan.text)
        return 1
    plan_id = plan.json()["id"]
    c.post(f"/api/plans/{plan_id}/approve").raise_for_status()
    job = c.post(f"/api/plans/{plan_id}/jobs", json={"name": "bounded live check", "target": "google_maps", "method": "deterministic_browser", "worker_count": 1, "requests_per_minute": 2, "max_captures": 2, "max_duration_minutes": 10, "max_attempts": 1})
    if job.status_code != 200:
        print("job refused:", job.text)
        return 1
    jid = job.json()["id"]
    c.post(f"/api/jobs/{jid}/start").raise_for_status()
    print(f"job {jid} started; waiting (watch the dashboard Run tab)...")
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("completed", "paused", "cancelled"):
            break
        time.sleep(3)
    print("status:", j["status"], j.get("pause_reason") or "", j["task_counts"])
    for o in c.get(f"/api/jobs/{jid}/observations").json()["items"]:
        d = c.get(f"/api/observations/{o['id']}").json()
        print("-", d["intended_side"], d["verification_status"], d["association_status"], d["quality_flags"])
        print("  camera:", d["camera_position"], "| pano:", d["panorama"])
        print("  observed orientation:", d["orientation"]["observed"])
        print("  imagery date:", d["imagery_date"], "| attribution:", d["attribution"])
        print("  image:", d["image"]["path"])
    for t in c.get(f"/api/jobs/{jid}/tasks").json():
        if t["status"] in ("failed", "no_imagery", "needs_review"):
            print("task", t["ordinal"], t["status"], t["outcome_reason"] or t["last_error"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
