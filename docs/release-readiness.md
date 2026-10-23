# Prototype: 0.2.0

External review approved prototype publication conditional on excluding vendor/testdata
from formatting checks. That correction has regression coverage in the real Docker test.
No live Jira/GitHub/model end-to-end pilot has been completed.

## Findings from the first review

| # | Current status | Evidence or limitation |
|---|---|---|
| 1 | Fixed | Non-login shell; real Docker finds and runs Go |
| 2 | Fixed | Configurable 1 GiB file ceiling; real binary exceeds the old 64 KiB limit |
| 3 | Fixed with retention limitation | Persistent isolated caches; reviewer rounds reuse ticket/role/image cache; no automatic quota/cleanup |
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
| 14 | Improved | New profiles check gofmt/build/test/race/vet; no baseline/tidy policy or extra linter |
| 15 | Improved | Bounded safe read/model retries and deferred remote work; no prompt caching |
| 16 | Fixed | Sensitive basename matching at any depth; nested environment-file regression |

Human-only merging replaces the previous auto-merge feature. There is no production merge
API; true auto_merge config is rejected. Jira approval commands cannot authorize anything.

## Follow-up review fixes

Remote retries now count consecutive failures, resetting after successful work or PR polls.
The regression alternates more transient failures than the budget with successful polls,
then observes a human merge. Separate checks cover work recovery, terminal-state recovery,
and avoiding a reset against a superseded specification.

Reviewer rounds retain a ticket/role/image cache across fresh clones; coder/verifier caches
remain separate. Real Docker checks cover reuse and formatting/build defaults, including
failure on unformatted and syntactically invalid Go files. Existing profiles are not rewritten.
GitHub secondary rate-limit responses defer without blindly repeating writes; ordinary
permission-denied responses are not classified as rate limits.

The formatting gate enumerates Go files explicitly instead of passing directories to
gofmt. It excludes root vendor/, all testdata/ directories, and hidden paths. The Docker
fixture contains unformatted vendor code and deliberately invalid testdata/hidden files;
verification succeeds without changing them. Unformatted or invalid project source still fails.
Existing profiles need their formatting command updated; generated profiles use the new gate.

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
