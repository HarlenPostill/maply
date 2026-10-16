"""Browser-driven demonstration of the main workflow (synthetic target only).

Drives the real dashboard in Chromium:
  draw route (coordinates) -> generate plan -> approve -> create job -> start one worker
  -> captures appear -> pause -> resume -> restart the worker process -> completion
  -> coverage -> gallery inspection + review -> local export.

Screenshots of each step are written to DATA_DIR/demo/<timestamp>/ (local only).

    cd backend
    uv run python ../scripts/demo_workflow.py --restart-cmd "docker compose restart worker"

The restart command simulates a worker/container failure mid-job; for a host
worker pass e.g. --restart-cmd "pkill -f app.workers.main; sleep 1; (uv run python -m app.workers.main >/tmp/w.log 2>&1 &)".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

# A short stretch of George St, Sydney CBD (drawn northbound), inside the approximate scope.
ROUTE = "151.20668,-33.87501\n151.20681,-33.87420\n151.20690,-33.87360"


class Demo:
    def __init__(self, page: Page, base: str, out: Path):
        self.page, self.base, self.out = page, base, out
        self.n = 0
        self.api = httpx.Client(base_url=base, timeout=20)
        self.log: list[dict] = []

    def shot(self, name: str, note: str = "") -> None:
        self.n += 1
        p = self.out / f"{self.n:02d}-{name}.png"
        self.page.screenshot(path=str(p), full_page=False)
        self.log.append({"step": self.n, "name": name, "note": note, "file": p.name})
        print(f"[{self.n:02d}] {name}: {note}")

    def tid(self, t: str):
        return self.page.get_by_test_id(t)

    def tab(self, name: str):
        self.tid(f"tab-{name}").click()
        self.page.wait_for_timeout(600)

    def job(self, jid: str) -> dict:
        return self.api.get(f"/api/jobs/{jid}").json()

    def wait_job(self, jid: str, cond, timeout=240, what=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.job(jid)
            if cond(j):
                return j
            time.sleep(1.0)
        raise SystemExit(f"timed out waiting for {what}: {self.job(jid)['status']} {self.job(jid)['task_counts']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default=None)
    ap.add_argument("--restart-cmd", default=None, help="shell command that restarts the worker process/container")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    status = httpx.get(a.base + "/api/system/status").json()
    # ./data is the same directory the containers mount at /data.
    data_dir = Path(status["storage"]["data_dir"])
    if not data_dir.exists():
        data_dir = Path(__file__).resolve().parents[1] / "data"
    out = Path(a.out or data_dir / "demo" / datetime.now().strftime("%Y%m%dT%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not a.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        d = Demo(page, a.base, out)
        page.goto(a.base + "/")
        page.wait_for_load_state("networkidle")

        # --- project
        projects = d.api.get("/api/projects").json()
        if not any(p["name"] == "Demo: George St pilot" for p in projects):
            if projects:
                d.api.post("/api/projects", json={"name": "Demo: George St pilot", "config": {"approval_reference": "DEMO (synthetic)"}})
                page.reload()
            else:
                d.tid("create-project-name").fill("Demo: George St pilot")
                d.tid("create-project-submit").click()
        page.wait_for_timeout(800)
        pid = next(p["id"] for p in d.api.get("/api/projects").json() if p["name"] == "Demo: George St pilot")
        d.tid("project-select").select_option(pid)
        page.wait_for_timeout(800)
        d.shot("dashboard", f"processing mode badge: {status['processing_mode']['label']}")

        # --- plan
        d.tab("plan")
        d.tid("route-coords").fill(ROUTE)
        d.tid("add-route-coords").click()
        page.wait_for_timeout(1000)
        d.tid("plan-interval").fill("20")
        d.tid("plan-views-per-side").select_option("1")
        d.tid("generate-plan").click()
        expect(d.tid("plan-summary")).to_contain_text("samples", timeout=15000)
        page.wait_for_timeout(1500)
        d.shot("plan-preview", "segments with direction, footpath offsets, samples, proposed headings, crossing approaches")
        d.tid("approve-plan").click()
        page.wait_for_timeout(1200)
        d.shot("plan-approved", "approved plan is immutable")

        # --- job
        d.tab("run")
        page.wait_for_timeout(800)
        d.tid("job-target").select_option("synthetic")
        d.tid("job-method").select_option("deterministic_browser")
        d.tid("job-workers").fill("1")
        d.tid("job-rate").fill("30")
        d.tid("create-job").click()
        page.wait_for_timeout(1500)
        jobs = d.api.get(f"/api/projects/{pid}/jobs").json()
        jid = jobs[0]["id"]
        d.tid("job-start").click()
        j = d.wait_job(jid, lambda j: j["captures_current"] >= 4, what="first captures")
        page.wait_for_timeout(2500)
        d.shot("running", f"worker live screenshot; captures={j['captures_current']} tasks={j['task_counts']}")

        # --- pause / resume
        d.tid("job-pause").click()
        j = d.wait_job(jid, lambda j: j["status"] == "paused" and j["task_counts"].get("running", 0) == 0, what="pause")
        caps = j["captures_current"]
        time.sleep(4)
        assert d.job(jid)["captures_current"] == caps, "captures continued while paused"
        page.wait_for_timeout(1500)
        d.shot("paused", f"paused by operator; progress held at {caps} captures")
        d.tid("job-resume").click()
        d.wait_job(jid, lambda j: j["captures_current"] > caps, what="progress after resume")
        d.shot("resumed", "collection continues")

        # --- restart recovery
        if a.restart_cmd:
            before = d.job(jid)["captures_current"]
            subprocess.run(a.restart_cmd, shell=True, check=False)
            d.shot("worker-restarted", f"worker restarted mid-job at {before} captures")
        j = d.wait_job(jid, lambda j: j["status"] == "completed", timeout=420, what="completion")
        page.wait_for_timeout(2500)
        d.shot("completed", f"tasks={j['task_counts']} captures={j['captures_current']} attempts={j['attempts_total']}")

        # --- coverage
        d.tab("coverage")
        page.wait_for_timeout(2500)
        cov = d.api.get(f"/api/jobs/{jid}/coverage").json()
        d.shot("coverage", f"completeness={cov['collection']['completeness']} verified={cov['verified_coverage']} gaps={len(cov['gaps'])}")

        # --- gallery + review
        d.tab("gallery")
        page.wait_for_timeout(2000)
        d.shot("gallery", "local thumbnails with status / association badges")
        d.tid("gallery-item").first.click()
        expect(d.tid("obs-detail")).to_be_visible(timeout=10000)
        page.wait_for_timeout(1200)
        d.shot("observation-detail", "requested vs camera position, provenance, imagery date vs collection time, checksum")
        d.tid("review-accept").click()
        page.wait_for_timeout(1500)
        d.shot("reviewed", "operator accepted the observation")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # --- export
        d.tab("export")
        page.wait_for_timeout(800)
        d.tid("export-create").click()
        page.wait_for_timeout(2500)
        exports = d.api.get(f"/api/projects/{pid}/exports").json()
        d.shot("export", f"local package at {exports[0]['absolute_path'] if exports else '?'}")
        browser.close()

    obs = d.api.get(f"/api/jobs/{jid}/observations", params={"limit": 2000}).json()
    summary = {
        "job_id": jid,
        "final": d.job(jid)["task_counts"],
        "observations": obs["total"],
        "duplicates": "checked by API: one current observation per planned view (unique index)",
        "steps": d.log,
    }
    (out / "demo-summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nscreenshots + summary in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
