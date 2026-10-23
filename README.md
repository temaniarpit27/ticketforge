# TicketForge

A local, Jira-first coding workflow: a ticket becomes a code change, a separately run
reviewer checks it, and a PR waits for your approval. Jira is the conversation and status
hub throughout. The service runs on your machine; code execution runs in Docker.

**Initial target: standalone Go repositories with one root `go.mod`.** TicketForge itself
is written in Python. Integrate another Go repository by generating a separate profile;
the shared coder/reviewer skills do not need to be rewritten for each project. Other
languages, Go workspaces, and local module replacements are explicitly blocked for now.

This is a working initial implementation with real Jira Cloud, GitHub REST, and Anthropic
Messages API adapters. An offline demo and tests exercise the workflow without accounts.
Live integration requires your own configuration and credentials; it is not activated by
installing this project.

## What it does

- Discovers opted-in Jira issues through a configurable JQL filter.
- Reads ticket descriptions and authorized human comments; excludes its own bot comments.
- Asks clarification questions on the ticket, pauses, and resumes after your answer.
- Accepts new input during coding, review, and PR review. Requirements have version numbers.
- Runs a coder and reviewer in independent model sessions and separate Git checkouts.
- Discovers the Go module, toolchain, directory structure, project guides, and conventions
  before asking either agent to work; supplies the same project facts to the reviewer.
- Gives the reviewer the specification and fixed commit, without the coder's explanation.
- Runs configured verification commands independently of the coder's completion claim.
- Returns concrete review findings to the coder, within a bounded repair loop.
- Pushes a ticket branch, creates or updates one PR, and adds the PR link to Jira.
- Posts work-started, clarification, failed checks, review, PR, CI/review updates, and merge
  completion as Jira comments. These comments have Jira's normal timestamps.
- Records your Jira approval for an exact full commit SHA and specification version.
- Optionally merges after approval, named CI checks, independent review, and GitHub rules.
- Persists tasks, event history, and outgoing Jira comments in SQLite. Reconciles uncertain
  writes and recovers incomplete work after restart.

Jira workflow columns/status fields are not changed automatically: every project has its
own transition rules. Status reporting is through comments. GitHub is where the code diff
and PR live; questions, answers, change requests, and approval can all stay on Jira.

## Quick offline demo

Python 3.9+ and Git are sufficient. No third-party Python runtime dependencies are needed.
From this project directory:

```sh
python3 -m ticketforge demo
```

The demo creates a new isolated example repository and SQLite database under
`.ticketforge/demos/`. It exercises clarification, a reviewer-detected boundary bug,
repair, PR creation, a changed requirement, rejected stale approval, and simulated merge.
It saves `jira-transcript.json` inside that run's directory.

**Demo actors and external services are scripted.** It uses real local Git operations and
tests a tiny internal Python fixture to exercise the coordinator, but makes no network
calls and does not execute an LLM. That fixture is not live Python-repository support.
There is no unrestricted host-execution mode for real agents.

## Configure a real run

### 1. Onboard a Go repository

From the TicketForge directory:

```sh
python3 -m ticketforge init --repo /absolute/path/to/your-go-repository
```

This creates `profiles/<repository-name>/` containing:

- `config.json`: repository-specific integration settings and Go verification commands.
- `Dockerfile.sandbox`: a toolchain image matched to `go.mod`/`toolchain`, with dependencies
  downloaded during build and runtime network access disabled.
- `PROJECT.md`: a project guide you can fill in without modifying the target repository.
- `discovered-context.json`: the initial repository facts for your inspection.
- `.env.example`: the credential variable names.

GitHub owner/repository is inferred from `origin`; override with `--github-repo owner/repo`.
Choose a different profile location with `--directory /path/to/profile`. Existing nonempty
profiles are never overwritten. The target repository is not changed by onboarding.

```sh
cp profiles/your-go-repository/.env.example profiles/your-go-repository/.env
chmod 600 profiles/your-go-repository/.env
```

Fill the Jira, model, and check-name placeholders in the generated `config.json`. The
default verification commands are `go test ./...`, `go test -race ./...`, and `go vet ./...`.
Adjust them if the project needs fixtures or a different supported verification procedure.
For manual setup, `config.example.json` and `.env.example` are also provided.

Review these fields in your profile's `config.json`:

| Field | What to enter |
|---|---|
| `repo` | Absolute path to an existing checkout of the target repository |
| `github_repo` | Its GitHub `owner/repository`; must match the repository you intend to change |
| `base_branch` | Target branch, usually `main` |
| `jira_url` | Your Jira Cloud site URL |
| `jira_jql` | The exact issues that opt into the agent; start with one test project and label |
| `allowed_jira_accounts` | Jira account IDs of humans allowed to instruct and approve |
| `coder_model`, `reviewer_model` | Anthropic model IDs available to your account; may be the same or different |
| `verification_commands` | Meaningful build/test commands for the target repository |
| `sandbox_image` | Image containing that repository's toolchain and dependencies |
| `project_guide` | Optional local guide; generated profiles point to their own `PROJECT.md` |
| `required_checks` | Exact GitHub check/context names required before automatic merge |
| `protected_paths` | Files/directories the coordinator refuses to commit; defaults protect CI workflows, submodules, and environment files |
| `auto_merge` | Defaults to `false`; enable deliberately after repository rules are configured |

Use a **separate Jira bot account**. Otherwise your own comments are indistinguishable
from bot messages. The service rejects configurations that authorize the bot as a human.
Only comments from listed accounts become instructions or approvals. Ticket summary and
description are also task inputs; rely on Jira project permissions to control who edits them.
Unlisted teammates can see the conversation but their comments do not drive implementation.

Set the profile's `.env` values:

```text
JIRA_EMAIL=the-bot-account-email
JIRA_API_TOKEN=the-bot-api-token
GITHUB_TOKEN=your-fine-grained-repository-token
ANTHROPIC_API_KEY=your-model-api-key
```

Values are read as literal strings, never evaluated as shell commands. Existing environment
variables take precedence. Do not commit `.env`, `config.json`, or `.ticketforge/`.

Permissions:

- Jira bot: browse chosen issues, read/add comments, add remote links.
- GitHub token: target repository contents read/write, pull requests read/write, checks and
  commit statuses read. No administration or branch-rule bypass is needed.
- Anthropic key: Messages API access for the configured models.

The GitHub integration uses a token for HTTPS fetch/push through a host-only askpass helper.
No token is embedded in the remote URL or mounted into the coding container. The service
uses the existing local checkout as an initial clone source and fetches the configured
GitHub base before starting a ticket. Submodules are not initialized automatically.

### 2. Prepare the execution image

Start your Docker engine, then build:

```sh
docker build -f profiles/your-go-repository/Dockerfile.sandbox \
  -t ticketforge-your-go-repository:local /absolute/path/to/your-go-repository
```

Use the exact image name in the profile's `sandbox_image`. The generated Dockerfile copies
only the module manifests, not your entire checkout or local `.env`. It preloads the Go
module cache. The repository is mounted separately when an agent runs. Agent commands have
no network access, so a new uncached dependency will require rebuilding the image. Do not
bake service credentials into it. Private modules, native libraries, database-backed tests,
and other unusual build requirements need an operator-prepared image/test environment.
The root `Dockerfile.sandbox` is a minimal Go reference; prefer the generated profile image.

### 3. Check and start

```sh
python3 -m ticketforge doctor --config profiles/your-go-repository/config.json
python3 -m ticketforge serve --config profiles/your-go-repository/config.json
```

`doctor` checks configuration, credential presence, Docker, and the image. It does not
validate remote credentials or perform remote writes. `serve` is the foreground worker;
keep it running to process tickets. Stop with Ctrl+C. Restart with the same command to
recover persisted state. Only one process can own a state directory.

```sh
python3 -m ticketforge status --config profiles/your-go-repository/config.json
```

This prints local task state for diagnosis. Normal use takes place on Jira, not through
a second chat interface. Polling defaults to 15 seconds; no public webhook server or tunnel
is required. Each running profile is single-worker and single-repository, Jira Cloud + GitHub.
Use separate profiles, state directories, sandbox images, and nonoverlapping Jira filters for
different repositories. There is no automatic multi-repository ticket router in this version.

## What happens without project context?

The workflow does not assume the model already knows your project. At every implementation
attempt it inspects the actual checkout's `go.mod`, root/two-level structure, and available
`README.md`, `AGENTS.md`, `TICKETFORGE.md`, `CONTRIBUTING.md`, and architecture documents. A
configured profile guide is also supplied to both agents. Each document has a source path
and a truncation flag. The agent then searches relevant code, applicable nested instructions,
and tests using its sandbox tools; this initial discovery is a map, not full code understanding.

- **Go code but little documentation:** continue investigating code and tests. Missing docs
  alone are not a reason to ask a long generic questionnaire or invent architecture.
- **Missing product behavior:** ask precise questions on Jira and wait for material answers.
- **Unknown/non-Go language or Go workspace:** post an explicit Jira blocker before model
  implementation. Add a tested language adapter in a later version.
- **Missing dependencies or external test services:** stop with evidence and ask for the
  required image/environment. Do not claim the change was verified.

For a useful `PROJECT.md`, describe the service purpose, package/entry-point map, important
invariants, external systems, test prerequisites, and compatibility boundaries. Keep facts
short and current; do not copy the entire repository. Alternatively keep shared guidance in
`TICKETFORGE.md` or `AGENTS.md` committed to the repository's target branch. An uncommitted
local guide will not appear in a fresh remote-base checkout; the profile guide avoids that.
If you edit the profile guide during an active task, add a Jira comment to trigger a new
attempt with the updated context.

## How to use a ticket

1. Write the desired behavior, constraints, and acceptance criteria in a Jira ticket.
2. Apply the label/filter configured by `jira_jql`, for example `ticketforge`.
3. Read the bot's work-started comment and answer clarification questions as ordinary comments.
4. Add requirements or corrections at any time. After the next poll they supersede the old
   task version; stale agent results are discarded before further tool use/publication.
5. Follow the PR link posted on Jira and inspect the diff. Request changes on the Jira ticket.
6. When satisfied, post `/forge approve FULL_COMMIT_SHA` using the SHA in the latest bot comment.
7. If automatic merge is enabled, the coordinator checks CI and repository state and attempts
   a merge using GitHub's exact-head SHA condition. Otherwise merge manually; completion is
   still reflected on Jira.

| Jira comment | Effect |
|---|---|
| Ordinary text | New requirement, clarification answer, or change request |
| `/forge pause` | Stop at a safe boundary; invalidate existing approval |
| `/forge resume` | Resume against current requirements |
| `/forge retry` | Retry after an infrastructure blocker; reset attempt budget |
| `/forge approve <40-character SHA>` | Approve the current independently reviewed PR commit |

Any changed human note is conservatively treated as a requirement change. This includes
edits/deletions of earlier notes. Repeated polls of unchanged comments do not create work.
Answers need not be final: the coder or reviewer can ask another question later.

Do not use casual comments such as "looks good" to authorize merging. The explicit command
avoids confusing discussion with approval. Approval must be posted after the PR is ready.

## Repository merge rules

Configure GitHub to require appropriate CI checks and human review if your project requires
it. Enable dismissal of stale approvals or approval of the latest reviewable push. Do not
grant the service branch-rule bypass privileges. Jira approval records your intent; it does
not manufacture a GitHub review or override required GitHub approvals.

Automatic merge requires all configured `required_checks` to report success, the exact
independently reviewed commit/specification, an explicit Jira approval, no outstanding human
change-request review, and GitHub's clean/mergeable state. GitHub rules remain authoritative.
The initial implementation supports squash merge; the repository must allow it.

## Skills and agent responsibilities

Reusable skills live in:

```text
ticketforge/skills/clarify-ticket/SKILL.md
ticketforge/skills/implement-change/SKILL.md
ticketforge/skills/review-change/SKILL.md
```

Edit these to refine how agents reason about the task. They are model instructions, not
permission enforcement. The runtime loads the coder's clarification/implementation skills
or the reviewer's review skill into a fresh role-specific session.

The reviewer gets the current specification, original base SHA, implementation head SHA,
and verification commands. It reconstructs behavior from the diff, may run its own checks,
and must provide concrete findings. Its checkout is disposable: reviewer edits never enter
the implementation. A separate session reduces anchoring but **does not guarantee unbiased
or complete review**. Human review is still required.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

The tests use scripted agents and fake remote services plus actual local Git and example
test runs. They cover mid-run input, independent review, repair limits, duplicate delivery,
durable recovery, stale approvals, CI failures, changed PR heads, and uncertain merge results.
They also inspect Docker restrictions and exercise the model tool-call protocol with fake
API responses. They do not prove that a real model will solve a particular ticket or that
your remote credentials and repository policies are correct.

## Boundaries and current limitations

- Docker commands use no network, no service credentials, a read-only root filesystem,
  resource limits, and a read-only Git metadata mount. The repository workspace is writable.
  Docker is a practical isolation layer, not a guarantee against hostile code or kernel exploits.
- Run against repositories you trust and an isolated Docker installation. Source-controlled
  secrets remain visible to an agent if you put them in the repository. Model requests contain
  ticket and code context: choose API/account settings appropriate for that data.
- Model calls are bounded by per-run steps/context/output tokens; they can still incur costs.
  No hard dollar accounting is implemented. A new human input or `/forge retry` creates a new budget.
- A paused or superseded model call is allowed to return, then discarded; an executing container
  is stopped. API calls already sent cannot be unbilled or undone.
- Jira and GitHub cannot commit atomically together. The coordinator re-reads Jira before merge
  and posts a final cut-off message. Input arriving during a successful remote merge is preserved
  and explicitly reported as follow-up work; it cannot retroactively change that commit.
- Outgoing Jira comments use a durable outbox and bot-owned markers for reconciliation. There
  is no distributed exactly-once guarantee across Jira and SQLite. Use one service process.
- Review/check status is mirrored from GitHub to Jira. Inline GitHub comments are not ingested
  as implementation instructions: write requests on Jira. Attachments and external links are
  not fetched automatically. Document text uses basic Atlassian Document Format extraction.
- Closed/merged tickets do not automatically reopen. Create a new ticket for follow-up work.
  External edits to the agent's branch block automatic merge and require manual reconciliation.
- Workspaces and traces are retained for inspection and can grow. Stop the service before
  archiving finished workspaces. A summary/trace is not a durable model session; recovery uses
  current requirements, saved files/commits, and fresh model calls.
- SQLite, logs, and traces contain private task/code content. Keep the state directory local
  and protected. The CLI creates files with a restrictive permission mask.

See [docs/architecture.md](docs/architecture.md) for the state machine and extension points.
