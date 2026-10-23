# TicketForge

A local Jira-to-PR coding agent for standalone Go repositories. Jira holds the requirements,
questions, answers, and progress updates. A coder implements the ticket, a separate reviewer
inspects the commit, and you approve and merge the PR on GitHub.

**Prototype:** TicketForge is a learning project and demo of agent orchestration. The initial integrations
are **Jira Cloud, GitHub, and Anthropic Messages**. No live-account end-to-end pilot has been
completed. The offline demo uses scripted actors; real Docker tests verify Go execution.

## Workflow

1. The local worker discovers a ticket matching your Jira filter.
2. A read-only assessment inspects the repository and posts a plan or material questions.
3. You answer on Jira. You can add requirements during any active stage.
4. The coder edits an isolated checkout. The coordinator runs configured checks.
5. A fresh reviewer session examines the specification and fixed commit in a separate checkout.
   Evidence-backed findings return to the coder within bounded repair rounds.
6. The worker pushes a branch, creates or updates a PR, and posts its link on Jira.
7. You review, approve, and merge on GitHub. The worker observes completion and reports it on Jira.

New authorized Jira input increments the specification version. Old tool results are discarded
at the next checked boundary; changed work needs fresh verification and review. Already sent
remote requests cannot be undone. Human merges are authoritative: later input becomes follow-up work.

**TicketForge has no merge API.** GitHub checks and review status are mirrored to Jira, but
GitHub branch rules and your review determine whether to merge. Jira workflow columns are
not changed; status is reported through timestamped comments.

## Try the offline demo

Requires Python 3.9+ and Git. There are no third-party Python runtime dependencies.
Linux and macOS are the initial host targets; the service uses POSIX process and file locks.

```sh
python3 -m ticketforge demo
```

This exercises clarification, a review finding, repair, PR creation, changed requirements,
and a simulated human merge. It saves a transcript and real local Git checkouts under
`.ticketforge/demos/`. The tiny internal Python fixture is a coordinator test, not a
supported Python language adapter. No model, remote service, or Docker is used by the demo.

## Connect a Go repository

From the TicketForge checkout:

```sh
python3 -m ticketforge init --repo /absolute/path/to/your-go-repository
```

The target checkout is unchanged. A new `profiles/<repository-name>/` contains
`config.json`, `Dockerfile.sandbox`, `PROJECT.md`, `discovered-context.json`,
and `.env.example`. GitHub owner/repository is inferred from origin; override with
`--github-repo owner/repository`. Use `--directory /path/to/profile` for another location.
Nonempty profiles are never overwritten.

```sh
cp profiles/your-go-repository/.env.example profiles/your-go-repository/.env
chmod 600 profiles/your-go-repository/.env
```

Fill the local profile and credentials. Root `config.example.json` and `.env.example`
are alternatives for manual setup.

| Setting | Purpose |
|---|---|
| `repo`, `github_repo`, `base_branch` | Existing target checkout, GitHub owner/repository, and base branch |
| `jira_url`, `jira_jql` | Jira Cloud site and explicit opt-in filter; start with one test ticket |
| `allowed_jira_accounts` | Account IDs whose comments may instruct the agent |
| `coder_model`, `reviewer_model` | Anthropic model IDs available to your account |
| `verification_commands` | Generated defaults check formatting, build, tests, race tests, and vet |
| `sandbox_image` | Toolchain/dependency image for this repository |
| `project_guide` | Optional external project context; generated profiles use `PROJECT.md` |
| `commit_author_name`, `commit_author_email` | Optional author override; otherwise GIT_AUTHOR variables, then your Git identity |
| `poll_seconds`, `status_poll_seconds` | Jira polling (15s) and waiting PR status polling (30s) |
| `protected_paths` | Paths the coordinator refuses to commit, including nested environment files |
| `max_agent_steps`, `max_review_rounds`, `max_verification_repairs` | Separate bounded execution/repair limits |

`auto_merge` is retained only for config compatibility and must be false. `required_checks`
is a legacy field; it does not enforce merge policy. Configure required checks on GitHub.

Use a **separate Jira bot account**; it cannot also be an authorized human account.
Only allowlisted human comments become instructions. Ticket summary/description are inputs
too, so Jira project permissions must restrict who can edit them.

```text
JIRA_EMAIL=the-bot-account-email
JIRA_API_TOKEN=the-bot-api-token
GITHUB_TOKEN=your-repository-token
ANTHROPIC_API_KEY=your-model-api-key
```

Values are literal strings; existing environment variables take precedence. Keep secrets in
the ignored `.env`, never in example files or images. Needed permissions:

- Jira: browse issues, read/add comments, add remote links.
- GitHub: target contents and pull requests read/write; checks/statuses read.
- Anthropic: Messages API access for your models.

Host Git uses an HTTPS askpass helper. Tokens stay on the host and are not included in
remote URLs or mounted into tool containers. Do not give the bot branch-rule bypass rights.
Generated target commits retain the configured author and include a “Generated by TicketForge”
message so automation is visible without inventing a coauthor identity.

## Build and run

Start Docker and build the generated image using the target repository as build context:

```sh
docker build -f profiles/your-go-repository/Dockerfile.sandbox \
  -t ticketforge-your-go-repository:local /absolute/path/to/your-go-repository
python3 -m ticketforge doctor --config profiles/your-go-repository/config.json
python3 -m ticketforge serve --config profiles/your-go-repository/config.json
```

Use the same image name in the profile. The generated Dockerfile copies module manifests
and preloads dependencies. Tool execution has no network. New dependencies require an image
rebuild; private modules, native libraries, external services, and special fixtures require
an operator-prepared environment.

`doctor` checks local config, credential presence, Docker, and an actual read-only Go
toolchain probe. It does not validate remote credentials. `serve` runs in the foreground;
Ctrl+C stops it. Restart with the same profile to reconcile persisted work.
`python3 -m ticketforge status --config <profile>` prints local diagnostic state.

The coordinator and Docker run on your machine; model inference runs at Anthropic.
Sleep/shutdown pauses progress. Each profile supports one repository and one worker.
Use separate profiles, state directories, images, and nonoverlapping Jira filters for
other repositories. No public webhook endpoint or automatic repository router is provided.

## Talking to the agent

Use ordinary Jira comments for answers, corrections, and implementation requests.
Answers can arrive at any active stage; the coder and reviewer can ask later questions too.

| Comment | Effect |
|---|---|
| Ordinary text | New specification input, including edits/deletions of earlier input |
| `/forge pause` | Invalidate active work and pause |
| `/forge resume` | Resume using current requirements |
| `/forge retry` | Retry after resolving a blocker; reset attempt limits |
| `/forge note TEXT` | Record a non-actionable note without restarting work |

`/forge approve` is unsupported. Approve and merge on GitHub.
GitHub inline comments are not implementation instructions: post change requests on Jira.

## Project context and reusable skills

At each attempt, discovery inspects the Go manifest, structure, root guides such as
`AGENTS.md`, `AGENT.md`, `agent.md`, `CLAUDE.md`, README and architecture docs,
plus the profile guide. It catalogs skills in `.agents/skills/` and `.claude/skills/`.
Agents read relevant bodies, source, tests, and applicable nested guides through sandbox tools.
Bounded discovery is a map; it does not mean the whole repository has been understood.

Fill `PROJECT.md` with purpose, package/entry-point map, invariants, test prerequisites,
external systems, and compatibility constraints. Missing docs alone should lead to code
investigation. Missing product decisions lead to questions on Jira. Unsupported layouts or
unavailable prerequisites lead to an explicit blocker. Local uncommitted target docs do not
appear in a fresh remote-base checkout; the external profile guide avoids that issue.
After changing a profile guide, add Jira input to trigger a fresh attempt.

The reusable role instructions are under `ticketforge/skills/`. Skills guide reasoning;
runtime permissions enforce boundaries. Separate reviewer context/checkouts reduce anchoring
but do not guarantee unbiased or complete review. Human review remains necessary.

See [extension contracts and templates](docs/extending.md) and [repository guide examples](examples/repository/).
Other trackers, GitLab, languages, multi-module Go workspaces, and local module replacements
are not supported by the initial adapters. Config strings do not automatically install plugins.

## Verification

```sh
python3 -m unittest discover -s tests -v
TICKETFORGE_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_docker_integration.py -v
```

The default suite uses fake APIs/scripted agents and real local Git. It tests changed input,
independent review, repair limits, recovery, reconciliation, schedules, context budgets,
HTTP retries, and protected paths. It inspects Docker arguments using mocks.
The separate opt-in test runs real Go test/race/vet as the container user, compiles a binary
larger than 64 KiB, checks persistent isolated caches, and verifies read-only mounts.
An explicitly requested Docker test fails if its engine/image build is unavailable.

## Current limits

- This is a demo candidate, not a production-proven autonomous engineer. A controlled live
  Jira/GitHub/model pilot and representative ticket evaluations are still needed.
- Reviewer findings are strings. There is no stable finding ledger, two-pass rebuttal
  reconciliation, or separate advisory-note schema; valid disagreements may consume rounds.
- New profiles check gofmt, build, test, race test, and vet. Existing profiles retain their
  configured commands; update them deliberately. Formatting excludes root vendor/, testdata/
  directories, and hidden paths, but can still expose preexisting problems in project source.
  No baseline comparison, automatic dependency-tidy policy, or extra linter is implemented.
- The base branch is fetched when creating a ticket workspace, not rebased automatically
  during repairs. Reconcile a stale base/conflicts manually and reverify/review changed code.
- Model sessions restart after interruption. Message history is bounded with recoverable
  observations, while canonical task input remains intact or fails explicitly if oversized.
  Limits exclude system/tool-schema overhead; there is no hard dollar budget or prompt cache.
- Reads use bounded transient retries. Writes are not blindly retried; known operations
  reconcile by stable identifiers where supported. Jira, GitHub, and SQLite are not atomic;
  no distributed exactly-once guarantee is claimed.
  Successful work/PR polls reset consecutive remote failures; sustained failures still block
  after the configured budget. GitHub secondary limits defer retries with increasing waits.
- Docker uses no network/credentials, read-only root/Git metadata, nonroot execution and
  resource limits. It is not a security guarantee against hostile repositories or kernel exploits.
  Source-controlled secrets remain readable. Ticket/code context is sent to the model provider.
- Workspaces, caches, SQLite, logs, and traces contain private data and grow until cleaned.
  Stop the worker before archiving/removing state. No automatic cache quota/retention policy exists.
- External PR branch changes block the agent for reconciliation. Merged/closed runs do not reopen.
  Jira attachments/external links are not fetched; rich text extraction is basic.
- A superseded API request may still complete and incur costs. Guard checks cannot undo a
  remote write already sent. New input observed after a human merge is reported as follow-up work.

See [architecture](docs/architecture.md), [review status](docs/release-readiness.md), and
[historical landscape/review](docs/landscape-and-review.md).
Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). License: [MIT](LICENSE).
