# Review candidate: 0.2.0

This is a local review candidate. Publication waits for the owner's confirmation that the
external review is ready. No live Jira/GitHub/model end-to-end pilot has been completed.

## Findings from the first review

| # | Current status | Evidence or limitation |
|---|---|---|
| 1 | Fixed | Non-login shell; real Docker finds and runs Go |
| 2 | Fixed | Configurable 1 GiB file ceiling; real binary exceeds the old 64 KiB limit |
| 3 | Fixed with retention limitation | Writable persistent role/workspace/image caches; isolation tested; no automatic quota/cleanup |
| 4 | Fixed | Waiting PR schedule defaults to 30s; mock call-count regression |
| 5 | Fixed with explicit bounds | Canonical task preserved; archived observations recoverable; oversized canonical input blocks |
| 6 | Known limitation | No stable findings/rebuttal ledger or second reconciliation pass |
| 7 | Fixed | Verification repair and review budgets are separate |
| 8 | Known limitation | Findings remain strings; no independent advisory channel |
| 9 | Fixed | Read-only assessment precedes coding for each new specification |
| 10 | Deliberate current scope | GitHub status mirrored; implementation requests belong on Jira |
| 11 | Fixed escape hatch | Ordinary comments remain inputs; /forge note records non-actionable notes |
| 12 | Known limitation | Base fetched at workspace creation; no automatic rebase during repairs |
| 13 | Improved | Go reasoning guidance added; no claim it enforces model quality |
| 14 | Known limitation | Default test/race/vet; formatting/build/lint policy configured by operator |
| 15 | Improved | Bounded safe read/model retries and deferred remote work; no prompt caching |
| 16 | Fixed | Sensitive basename matching at any depth; nested environment-file regression |

Human-only merging replaces the previous auto-merge feature. There is no production merge
API; true auto_merge config is rejected. Jira approval commands cannot authorize anything.

## Verification boundaries

The offline suite uses scripted agents and fake remote services, with actual local Git.
The opt-in real Docker suite builds the generated Go image, tests concurrent Go code using
test/race/vet as the host UID, checks the binary ceiling, role/check-out cache separation,
and read-only root/Git/assessment mounts. Doctor uses an actual Go sandbox probe.

These checks establish runtime mechanics, not whether a real model solves representative
tickets or whether live provider permissions/pagination/rate limits work for a specific tenant.
Before production use, pilot a small Go ticket with real accounts, inspect the diff, verify
comment edits/new input at multiple stages, then build an evaluation set.

## Public tree

MIT license, placeholder-only environment examples, ignored runtime/config/private directories,
and public setup/extension documentation are included. Private interview notes belong only
in ignored local state. Check the local final validation record for history-cleanup and
secret-scan results; this document does not claim scans pass merely because rules exist.

The historical review is preserved as an explicitly historical document. Its original
findings are not a statement that current fixes remain unimplemented.
