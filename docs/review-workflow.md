# Review the completed dataset

Maply's primary task is resolving uncertainty in its existing single job. The
workspace distinguishes saved photos needing interpretation from planned points
without usable imagery. Missing imagery, blocked views, and skipped shots cannot
become observed data merely by changing a status or asking an image model.

## AI second reviews

Start with the unconfirmed absent-footpath cohort. The original dataset contains
4,229 absent-footpath annotations; 3,262 have only the standardized absence-review
reason. The UI obtains current counts from the database rather than these figures.

An explicit Start action selects a bounded batch and freezes its OpenRouter model.
The dedicated review executor reuses provider settings and annotation transport,
without enabling ordinary automatic tagging. New Review next batches authorize
automatic acceptance: each valid result becomes the current full annotation and
its review is marked confirmed. Unknown is saved as unknown, with uncertainty
checks retained. Single-photo reviews and pre-existing batches retain manual
confirmation. Provenance distinguishes automatic acceptance from human checking.
Restart pauses unfinished batches. Uncertain requests
are not automatically replayed.

A proposal is bound to the image SHA256 and original-label fingerprint. Confirmation
checks both under locks and rejects changed evidence or competing tagging work.
Unknown observations remain unresolved even when their AI result is accepted.
A confirmed correction can replace the old
annotation with validated full tags; a confirmed absence remains an absent-footpath
data point. Rejecting a proposal does not certify the previous interpretation.

Confirmation removes only the standardized absence-review reason. Other capture,
framing, visibility, and annotation concerns remain visible. Annotation JSON and
database provenance must agree on the resulting labels and acceptance method.

## Recover missing evidence

Skipped, blocked, and no-imagery points offer the planned Street View and a local
extension recapture action. Preparing a point for recapture moves it to an explicit
recovery queue, retaining its prior status for context. Only a newly saved image
that passes the existing capture checks can be tagged as observed evidence.

## Architecture decision

The implementation uses separate review batches and items. The existing unique
TagTask per shot already represents ordinary annotation and its completion writes
canonical labels immediately. Extending that task's states would spread review
semantics through summaries, exports, and ordinary workers.

Two independent designs were compared. Candidate A's separate proposal records
were selected; candidate B contributed independent prompting, conservative unknown
handling, bounded explicit requests, and restart behavior. The cross-review agreed
with the separate-record design and identified the label-file consistency and
ordinary-tagging race as required checks.

Validation uses isolated PostgreSQL data and fake model responses. Production
photos and labels are not changed to demonstrate the workflow, and a bulk paid
review is started only through an explicit user action.
