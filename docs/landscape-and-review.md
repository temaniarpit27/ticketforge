# TicketForge: landscape, deployment, and implementation review

Reviewed on 23 October 2026 against commit `bd2e13e`. This is an assessment and proposed
improvement sequence; the runtime fixes below have not been implemented by this review.

## Where the agent runs

TicketForge currently runs as a foreground Python service on the machine where you invoke
`python3 -m ticketforge serve --config <profile>`. It is not installed as a background daemon
and is not hosted inside Jira or GitHub.

| Component | Current location and behavior |
|---|---|
| Coordinator | Local Python process; polls Jira, runs the workflow, talks to GitHub |
| Coder and reviewer reasoning | Separate Anthropic Messages API conversations; inference is remote |
| Code/tool execution | Docker on the same host; a new short-lived container per shell command |
| Implementation and review files | Separate checkouts on the host, mounted into their containers |
| Durable state | SQLite, outbox, traces, and workspaces under the profile's state directory |
| Human interaction | Jira comments and PR diff/review on GitHub |

While waiting for an answer, no model needs to remain running. The saved task waits and
the coordinator polls. If the host sleeps or the foreground process stops, polling and
progress stop. State survives, but processing resumes only when the service runs again.

Local execution is not local-only data processing: ticket/code context is sent to the model
provider. Container restrictions do not change that data boundary.

Recommendation: validate locally first. For continuous use, deploy one coordinator and
Docker engine on an always-on Linux VM with persistent storage and supervised restarts.
Run one profile per repository with nonoverlapping Jira filters. Keep model/service
credentials on the coordinator and out of tool containers. Kubernetes and distributed
scheduling are unnecessary for the first single-user pilot. This deployment is proposed,
not implemented or tested here.

## Similar systems and what their implementations teach us

These are relevant alternatives, not evidence that any product exactly matches every
TicketForge interaction. Public source was inspected for the open-source projects; Rovo
Dev was assessed from Atlassian documentation, not proprietary implementation code.

### Rovo Dev in Jira: the closest product alternative

Atlassian documents coding from Jira against connected GitHub Cloud or Bitbucket Cloud,
with branches/commits and draft PR handoff to humans. Its documentation describes hosted
sandbox sessions. Availability depends on the site's rollout, enablement, permissions,
and connected source control. Do not assume it is enabled on a particular tenant.

For a delivery-only goal, trial this before investing heavily in a custom Jira product.
The exact asynchronous comment-driven lifecycle and approval semantics we want still
need to be checked in a real trial; they were not established from these pages.

Sources: [Atlassian's product Q&A](https://community.atlassian.com/forums/discussion/3215401/webinar-q-a-rovo-dev-in-jira),
[hosted Jira coding session announcement](https://confluence.atlassian.com/cloud/blog/2026/08/atlassian-cloud-changes-aug-10-to-aug-17-2026).

### OpenHands: an alternative execution engine

The SDK separates agents, tools, conversations, and workspaces. It supports local and
remote workspace execution. Its documented context condenser preserves recent exchanges
and summarizes older material, and its conversation APIs support pause/resume. The SDK
README also separates execution from its automation repository's scheduling/dispatch duties.

The lesson is architectural: keep our Jira/policy coordinator separable from the coding
engine. A future OpenHands adapter is credible, but we should compare it on representative
Go tasks before replacing our current runtime. Its existence does not mean we can remove
Jira-specific state, approval policy, or independent review coordination.

Sources: [SDK architecture and repository boundaries](https://github.com/OpenHands/software-agent-sdk),
[condenser implementation](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py),
[pause/resume documentation](https://docs.openhands.dev/sdk/guides/convo-pause-and-resume).

### mini-SWE-agent: a useful small-runtime reference

Its Docker environment starts one container and executes subsequent commands with
`docker exec`, preserving the execution environment between commands. This contrasts with
our fresh-container-per-command design. Its interpreter and environment are configurable.

We can learn from its small execution interface without copying its defaults. Persistent
containers make caches and shell environment easier to retain, while our short-lived
containers simplify per-command cleanup. Persistent, role-isolated Go caches are a smaller
first improvement than changing the entire lifecycle. SWE-agent's own README now points
to mini-SWE-agent for new use.

Sources: [Docker environment source](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/environments/docker.py),
[SWE-agent guidance](https://github.com/SWE-agent/SWE-agent).

### Patchwork: composable issue-to-PR automation

Its `ResolveIssue` source composes issue reading, an investigative agent run, structured
extraction of the investigation, an implementation run, and a PR step. This is a concrete
example of splitting investigation and implementation instead of doing everything in one
unstructured conversation. It is not evidence of our exact Jira clarification loop.

We should borrow the phase separation, not blindly copy prompts or model defaults. In
particular, its inspected implementation's instruction to avoid changing tests is not a
policy I would adopt for TicketForge.

Source: [ResolveIssue implementation](https://github.com/patched-codes/patchwork/blob/main/patchwork/patchflows/ResolveIssue/ResolveIssue.py).

## Assessment of Claude's feedback

Claude identified real defects in the first implementation. Passing coordinator tests
does not establish that the Go sandbox can build a repository. Its priorities are broadly
right, but some proposed behavior changes need to preserve our explicit requirements.

| # | Finding | Assessment and recommendation |
|---|---|---|
| 1 | Login shell can reset Go's PATH | Credible image-dependent runtime risk. Our code uses `sh -lc`; change to `sh -c` and verify `command -v go` in the actual generated image. Docker was unavailable, so the image-specific failure is not reproduced here. |
| 2 | `fsize=65536` means a 64 KiB file ceiling | Confirmed configuration defect. Docker passes the raw resource-limit values to the OS. A local POSIX reproduction failed with errno 27 at exactly 65,536 bytes. Raise/configure the limit and test compilation of a binary larger than the old limit. |
| 3 | Cache and temporary disk vanish per command | Confirmed by execution arguments and image configuration. Use persistent caches isolated by repository/toolchain and role, with explicit capacity/cleanup policy. Do not share one writable cache between coder and reviewer. A 256 MiB temporary filesystem is a risk, not proof that every Go build exceeds it. |
| 4 | Waiting PRs are checked each second | Confirmed. Five ticks produced five Jira snapshots and five calls each to PR, checks, and reviews adapters. `checks` performs at least two GitHub REST calls, so one waiting ticket implies at least four GitHub requests per tick, about 14,400/hour at one-second ticks, before pagination. Schedule per-ticket checks and respect rate-limit responses. |
| 5 | History can exhaust the context budget | Confirmed limit and raw-history growth. Failure at exactly seven outputs is not universal: initial context and output sizes vary. Preserve canonical requirements/checkpoints outside summaries, keep recent tool exchanges, and bound or summarize older output with recoverable trace references. `max_agent_steps` is already configurable. |
| 6 | Reviewer lacks findings/rebuttal continuity | Confirmed. Preserve stable finding IDs and per-finding coder evidence. First perform independent inspection, then reconcile prior findings as resolved/open/withdrawn. Do not replace fresh assessment with the coder's narrative. |
| 7 | Verification failures consume review rounds | Confirmed. `round` counts whole attempts. Separate verification repair, review, and overall time/cost limits. |
| 8 | Passing review cannot carry minor notes | Confirmed schema limitation. Distinguish blocking defects from advisory notes, with severity and evidence. Notes should not force repeated coding attempts. |
| 9 | No enforced pre-edit clarification stage | Correct: a skill requests clarification, but runtime does not enforce read-only investigation first. Add a read-only assessment before first edits; allow later questions too. This does not require adding a third permanent agent. |
| 10 | GitHub feedback is not implementation input | Correct and currently documented. Jira-only discussion was an explicit requirement, so this is partly a product choice. Keep the rule visible in the PR; optionally ingest authorized GitHub findings with stable IDs and mirror them to Jira. Do not interpret arbitrary GitHub comments as approved instructions. |
| 11 | Every human comment restarts the task | Confirmed conservative behavior. Preserve ordinary new inputs at every stage. Add an explicit non-actionable note command if needed; do not restrict ordinary inputs to the clarification stage, which would violate the requested workflow. |
| 12 | Base branch never refreshes | Confirmed. Add an explicit coordinator-owned refresh/rebase path with conflict reporting. Changed code must undergo verification and review again; an old human approval cannot survive a changed head. |
| 13 | Skills lack Go-specific checks | Correct. Add formatting, errors, cancellation propagation, resource lifecycle, relevant concurrency/race checks, and compatibility guidance. Avoid a mechanical checklist that invents problems irrelevant to the diff. |
| 14 | Default verification can improve | Add formatting and build checks. A tidy/diff check should respect the repository's Go version, dependency/network setup, and preexisting baseline. Optional linters must be present in the image. Do not silently add tools that cannot run. |
| 15 | Transient API failures block work | Correct. Add bounded retries for appropriate errors, with jitter and Retry-After handling. Reconcile uncertain side effects before retrying Jira writes or merges; a blanket retry on all POST/PUT calls is unsafe. Prompt caching is a separate cost optimization. |
| 16 | Nested environment files are unprotected | Confirmed. Default matching protects `.env` but not `cmd/api/.env` or `cmd/api/.env.production`. Match sensitive basenames at all directory depths, and test normal/protected paths. |

Docker's resource-limit behavior is documented in its
[container run reference](https://docs.docker.com/reference/cli/docker/container/run).

## Verification performed for this review

- Re-ran the existing suite: 43 tests passed with Python 3.12.
- Reproduced the file-size resource limit in a child process using the host POSIX API.
  This verifies the units/ceiling; it is not a Docker test.
- Exercised five waiting-PR ticks with fake adapters and counted their calls.
- Evaluated the actual default protected-path matcher on root and nested `.env` names.
- Confirmed the Docker engine remains unavailable at the configured Colima socket.

No live Jira, GitHub, model, or Go-in-Docker execution was performed. The tests use scripted
agents and fake remote services; public implementation code inspection is not a substitute
for running those systems in our environment.

## Recommended improvement sequence

1. **Make real Go execution pass:** shell/PATH, file-size limit, writable cache capacity,
   cancellation/cleanup, and nested protected paths. Add a Docker integration test that
   compiles a small Go module and runs test/race/vet checks as the production container user.
   A skipped Docker test must be clearly distinguished from a passed release prerequisite.
2. **Make waiting inexpensive and recoverable:** per-ticket scheduling, API backoff,
   observable failures, durable retry state, and a baseline check before modifications.
3. **Make collaboration converge:** read-only assessment, structured finding IDs, independent
   first-pass review followed by reconciliation, advisory notes, separate budgets, and context
   compaction that preserves current requirements and tool-result pairing.
4. **Pilot a real Go repository:** a few scoped tickets, ambiguous inputs, changed requirements,
   restart/retry cases, and exact-head approvals. Keep automatic merging disabled initially.
5. **Evaluate adoption before expanding:** compare a current existing product/engine on the
   same tasks. Add another language or remote execution only after the Go pilot has evidence.

The first two steps address implementation defects, not differentiation. Product claims
should wait for the pilot and comparative evaluation described in
[differentiation-and-interview.md](differentiation-and-interview.md).
