# Operator guide

## Start

```bash
cp .env.example .env            # optional; defaults are safe (LOCAL ONLY)
docker compose up -d --build    # db, api, worker — all bound to 127.0.0.1
open http://127.0.0.1:8000
```

Check the header first. It shows the **processing mode** (`LOCAL ONLY` unless
you enabled cloud image processing) and the **data directory**.

## 1. Project and limits (Settings)

* Record the purpose, approval reference and approval expiry. Jobs on the live
  target are refused after expiry.
* Permitted access methods and targets, max workers, max requests/min,
  total capture limit, retention days, and the scope buffer.
* Third-party processing: leave off unless your approval permits it *and* the
  operator has set `ALLOW_CLOUD_IMAGE_PROCESSING=true`.
* Project settings can only narrow the operator ceilings in `.env`. The
  "effective limits" panel shows the result and why cloud processing is off.

## 2. Plan (Plan tab)

1. Draw a **route** along the street, in the direction you want to define as
   "forward". You can also enter coordinates, import GeoJSON (lines, or a polygon
   whose perimeter becomes a route), or paste a Google Maps Street View URL as a
   starting observation.
2. Choose sides (left/right/both, relative to the arrow), sampling interval
   (metres), views per side (perpendicular, + forward oblique, + backward
   oblique), pitch, FOV, viewport, imagery-age target and the unknown-date policy.
3. **Generate capture plan.** Review segments (with direction arrows),
   approximate footpath offsets, samples, proposed headings and intersection
   candidates. Drag samples to adjust them (they must stay within 25 m of their
   segment), exclude samples, or reset them.
4. **Approve.** Approved plans are immutable; to change anything, create a new plan.

## 3. Run (Run tab)

1. Create a job for the approved plan: target (`synthetic` for practice,
   `google_maps` for live), method, workers (default 1), request rate, capture,
   duration and model-budget limits. **Check permissions** explains any refusal.
2. **Start.** Worker cards show status, heartbeat, current task and a live,
   locally served screenshot.
3. **Pause** stops new requests. Workers release their current task, which counts
   as an interruption, not a failure. **Resume** continues. **Cancel** is final.
4. If the job pauses with **manual intervention** (consent, CAPTCHA, sign-in,
   provider safety check), use the worker's Manual control panel: click on the
   screenshot, use the key buttons, or *Skip*. Then press **Resume**. Workers never
   bypass these screens.
5. Limits (captures, duration, estimated budget, approval expiry) pause the job
   with the reason shown in the banner.

**Manual method.** The worker positions the browser at each sample. You use
*Go to view* / *Capture* for each planned view, then *Done*, or *No imagery* /
*Skip*.

## 4. Coverage

Two separate numbers:

* **Collection completeness:** tasks that reached any terminal outcome.
* **Verified coverage:** planned views with a capture that passed verification.

The map colours samples as verified, reused, needs review, missing, no imagery or
pending. Black dots are camera positions as reported by the source (**not**
feature locations). The gaps table lists stretches of each segment side with
no camera, measured from actual camera positions.

## 5. Review (Gallery)

Filter by status, flag, side or segment. Each observation shows requested vs
camera position, planned vs observed orientation (with provenance), imagery
date (with precision and provenance) vs collection time, file checksum, flags
and the verification evidence.

* **Accept.** Optionally tick "I confirm the intended footpath is visible", which sets
  the association to `operator_verified`.
* **Reject.** Optionally request a recapture of that view.
* **Recapture** requeues that view (a completed job reopens as paused; press Resume).
* **Crop.** Creates an analysis crop that references the raw image and its checksum.

Common flags: `imagery_date_unknown`, `imagery_older_than_desired`,
`camera_far_from_sample`, `heading_mismatch`, `observed_heading_on_other_side`,
`attribution_missing`, `panorama_id_unknown`, `scope_unverified`.

## 6. Export and retention

* **Export** writes `data/exports/<project>-<timestamp>/` with `manifest.json`
  (checksums, conventions, usage restrictions), `observations.jsonl`, plan GeoJSON,
  camera positions, coverage and config versions, plus images referenced by relative
  paths. Nothing is uploaded.
* **Retention** previews and deletes local data older than the project's retention
  period (requires typing the project name).

## Recovery behaviour

* Kill or restart anything at any time. On restart the supervisor detects dead
  worker slots, requeues their tasks and moves unreferenced image files to
  `data/orphans/`. Completed captures are never duplicated.
* `docker compose restart worker` is safe mid-job.
