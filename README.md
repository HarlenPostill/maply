# maply

Street accessibility data for Sydney, from collection to production.

| Folder | What it is | Runs on |
| --- | --- | --- |
| [`tagger/`](tagger/README.md) | The Maply capture, AI tagging and review system: Street View capture, OpenRouter annotation, review workspace, Chrome extension, iOS run monitor. It holds all 52,483 photos and their full annotations. | Your Mac (Docker, `127.0.0.1:8000`) |
| [`atlas/`](atlas/README.md) | The production database for consumer products. Street segments, footpath observations, per-side wheelchair grades, a routing graph and verification targets, with no photos or model confidence. | Railway (PostgreSQL 17, PostGIS, pgRouting) |

```
tagger (photos + full annotations, local)  ──  atlas publish  ──▶  atlas (facts only, Railway)
                                                                      ▲            │
                                                Petal Path players ───┘            └──▶ wheelchair routing
```

Data flows one way, from the tagger into the atlas. Petal Path will write community observations
straight into the atlas, and the atlas's derived views treat them exactly like survey
data. Re-publishing from the tagger, for example after a new capture job, replaces
only the survey layer and never touches player data.

Consumer apps (Petal Path, the routing app) will live alongside these two folders.
