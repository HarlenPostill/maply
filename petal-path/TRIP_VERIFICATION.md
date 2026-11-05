# Trip planning verification — 5 November 2026

Implemented locally in the main checkout. Nothing was committed, pushed or deployed.
Production database access was read-only. The tagger dataset and active Docker volume were not changed.

## Scope

Destination search, real or explicitly labelled simulated origins, route planning, an interactive
overview and readable evidence/warnings. Every result is an unverified preview with navigation off.
There is no live guidance. Release builds have no service URL until deployment is approved.

The survey covers the approximate City of Sydney boundary, not greater metropolitan Sydney.
The API rejects endpoints outside it and reports disconnected survey networks and endpoint gaps.
Crossings, kerbs, entrances and grade separation are not verified. Slopes are sparse categorical
observations, not measured gradients. Missing slope tags do not mean level ground.

## Verification

- Backend: 73 tests passed against an isolated `atlas_test` database, including routing,
  exclusions, endpoint matching, snapshot integrity, refresh recovery and cancellation.
  The disposable test container was removed.
- App: 70 unit tests passed (14 existing and 56 trip-planning tests).
- iPhone 17 Simulator, iOS 27: destination search, real/simulated start handling, API preview,
  expanded map, warnings, cancelled destination edits, stale-start replanning, disconnected
  destination and outside-coverage errors were exercised through T3 device tools.
- Largest accessibility text size: actual route warnings and leg descriptions inspected.
- Mac Catalyst: real Metal 4 renderer ran with API/GPU validation enabled. The route API
  returned successful coverage and preview responses, and the captured log contains no
  Metal validation errors.
- Independent Sol backend review and re-check completed; findings were fixed and tested.
- Separate single-pass Sol and Opus UX reviews completed; the UI Opus agent adapted both
  critiques and re-ran app tests and device validation.
- Final working-tree whitespace check passed.

## Not verified

Nothing has run on a real iPhone. Real-world wheelchair travel, physical route accessibility,
device GPS performance, cellular behaviour, thermal/battery performance and spoken VoiceOver
remain untested. Accessibility-tree checks are not a spoken VoiceOver test. Contrast was not
instrument-measured. Trip screens currently use light appearance.

## Local use and production

From `atlas/`, run `uv run python -m atlas serve`; Debug app builds use
`http://127.0.0.1:8765`. The temporary API process used for verification was stopped at handback.
The Simulator remains available in the Device panel.

Railway currently has only `atlas-db`. A production API requires approval to create a
least-privilege login and deploy an `atlas-api` service. Public exposure also needs rate limiting.
See [the API contract and deployment proposal](../atlas/ROUTING_API.md).

Screenshots and test/validation logs are retained locally under `build/trip-verification/`.
