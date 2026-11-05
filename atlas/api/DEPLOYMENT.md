# Railway route API

Deployed 6 November 2026 (Sydney time), with the user's approval.

- URL: https://atlas-api-production-662d.up.railway.app
- Railway project: `maply-atlas`; service: `atlas-api`; region: `ams` (one replica).
- Deployment: `1d975266-2637-4dd1-a06d-1676c3d342fd` — `SUCCESS`.
- Snapshot: release 1, graph `r1-257a820ed8e1` at verification.
- Database login: `atlas_api`, restricted to `atlas_read`, with read-only defaults,
  statement/connection limits and runtime privilege checks. Private networking with TLS.
- Secrets remain in private local files and Railway variables, never in the app or upload.

Verified public HTTPS health, coverage, a successful Sydney route preview, typed endpoint
rejection, hidden docs, no-store responses and shared rate limiting with rotating headers.
The successful route returned `unverified_preview` and `navigation_allowed: false`.
The backend suite passed 95 tests against a disposable database before deployment.

The signed Debug app `com.harlenpostill.PetalPath` version 0.1 (build 1) was installed
on the connected iPhone 17 named `harlen` after health returned ready. The device app list
confirmed installation. Its bundled service URL is the HTTPS address above, and the build
includes the 120-second request timeout. The app was not launched or controlled on the phone;
real-device rendering and interaction remain for the user's first test. App tests passed 78 cases.

This initial testing deployment shares one client-rate bucket across all callers; proxy
headers are not trusted. No Railway plan upgrade was made. Sleeping remains enabled, so
the first request after idle can take longer. iPhone requests allow up to 120 seconds.

Only the API service and its database login were added. No survey publish, schema migration,
database-service deployment or active dataset-volume change was performed.

Redeploy with `api/provision.sh` from `atlas/`. It stages an allowlist outside the repository.
The image enforces readable runtime-source permissions and checks imports as the non-root
runtime user during the build. `api/check.sh` checks the live endpoint after deployment.
