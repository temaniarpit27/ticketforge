# TicketForge

TicketForge is an AI teammate for Go projects that works from Jira. You assign it a ticket;
it asks questions if anything is unclear, writes the code, has a second AI review that code,
fixes what the reviewer finds, and opens a pull request. **You always review and merge.**

**Status: prototype.** It is a learning project. It works with Jira Cloud and GitHub, and
with any AI model you configure (Claude, OpenAI, Gemini, a local model, and so on). It has
been tested offline and in Docker, but not yet with real accounts on a real project.

## How it works, in plain words

Think of a small team: a developer, a reviewer, and you as the lead.

1. **You assign a Jira ticket to the bot account.** Only assigned tickets are picked up.
   Unassign it and the work stops (an already-open pull request is still watched).
2. **The developer reads first.** The coder AI looks at the ticket and the code without
   changing anything, then posts its plan on Jira, or asks you questions and waits.
3. **You answer on Jira.** You can add or change requirements at any time: a comment, an edit
   to the summary or description, or any field listed in `jira_spec_fields`. The work restarts
   on the new version, unfinished work on the old one is thrown away, and both AIs are shown
   the old requirements next to the new ones so they can see exactly what changed. If a pull
   request is already open, the new commit is added to the same pull request.
4. **The developer writes the code** in a private copy of the repository, inside a locked-down
   container with no internet access and no passwords.
5. **TicketForge runs the checks itself** (formatting, build, tests, race tests, vet). It does
   not take the AI's word that tests pass. Failures go back to the coder.
6. **The reviewer AI checks the change** on its own copy, without seeing the coder's reasoning.
   It can be the same model as the coder or a different one.
7. **They talk it through.** Every problem the reviewer finds gets an ID like `F1`. The coder
   must answer each one: "fixed", or "disagree" with proof. The reviewer then marks each as
   solved, still a problem, or withdrawn. If they keep disagreeing, **you decide** on Jira.
8. **A pull request is opened** once nothing serious is left, and its link is posted on Jira.
9. **You review and merge on GitHub.** TicketForge cannot merge; it only reports what happened.

It works on one ticket at a time and keeps going on it until it gets stuck (for example,
waiting for your answer); then it picks up the next ticket in line. See "Running it for a
team, on a server" below.

Jira holds the conversation (questions, answers, progress). GitHub holds the code. Jira
columns are never moved automatically; progress is posted as comments.

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
| `coder_provider`, `reviewer_provider` | Provider name per role: built-in `anthropic`/`openai`, or one you define in `model_providers` |
| `coder_model`, `reviewer_model` | Model ID per role; both roles may use the same provider and model |
| `model_providers` | Your own providers (see below) |
| `reviewer_max_output_tokens` | Reviewer output budget (25000; reasoning models need room) |
| `max_finding_disputes` | How many disagreements on one finding before you decide on Jira (1: ask you on the first one) |
| `require_agent_assignment` | Only work on tickets assigned to the agent's Jira account (true) |
| `jira_spec_fields` | Extra Jira fields that are part of the requirements, such as an acceptance-criteria custom field (`["customfield_10034"]`) |
| `verification_commands` | Generated defaults check formatting, build, tests, race tests, and vet |
| `sandbox_image` | Toolchain/dependency image for this repository |
| `project_guide` | Optional external project context; generated profiles use `PROJECT.md` |
| `commit_author_name`, `commit_author_email` | Optional author override; otherwise GIT_AUTHOR variables, then your Git identity |
| `poll_seconds`, `status_poll_seconds` | How often to read Jira (15 s; 60 s is fine for a team) and to check each PR waiting for review (120 s) |
| `protected_paths` | Paths the coordinator refuses to commit, including nested environment files |
| `max_agent_steps`, `max_review_rounds`, `max_verification_repairs` | Separate bounded execution/repair limits |

`auto_merge` is retained only for config compatibility and must be false. `required_checks`
is a legacy field; it does not enforce merge policy. Configure required checks on GitHub.

Use a **separate Jira bot account**; it cannot also be an authorized human account.
Hand a ticket to the agent by assigning it to that account (it must be assignable in the
project); unassign it to stop. Set `require_agent_assignment` to false to use the filter alone.
Only allowlisted human comments become instructions. Ticket summary/description are inputs
too, so Jira project permissions must restrict who can edit them.

```text
JIRA_EMAIL=the-bot-account-email
JIRA_API_TOKEN=the-bot-api-token
GITHUB_TOKEN=your-repository-token
ANTHROPIC_API_KEY=your-anthropic-key   # if a role uses anthropic
OPENAI_API_KEY=your-openai-key         # if a role uses openai
# plus the api_key_env of any custom provider you use
```

### Choosing models

You choose which AI writes the code and which AI reviews it. They can be the same model or
different ones. Each needs a **provider** (where to send requests) and a **model** (which AI).

`anthropic` and `openai` are built in. To use anything else, add it under `model_providers`.
Most AI services accept requests in one of three formats, so you only say which one:

| `api` | Format | Use it for |
|---|---|---|
| `anthropic-messages` | Anthropic Messages API | Claude models (built-in `anthropic`) |
| `openai-responses` | OpenAI Responses API, stateless | OpenAI models, incl. Codex (built-in `openai`) |
| `openai-chat` | OpenAI-compatible Chat Completions | Gemini, Mistral, DeepSeek, OpenRouter, Azure, Ollama, vLLM, LM Studio, gateways |

```json
"model_providers": {
  "gemini": {"api": "openai-chat", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
             "api_key_env": "GEMINI_API_KEY"},
  "ollama": {"api": "openai-chat", "base_url": "http://localhost:11434/v1", "auth": "none"}
},
"coder_provider": "anthropic", "coder_model": "<claude model id>",
"reviewer_provider": "gemini",  "reviewer_model": "<gemini model id>"
```

What each provider field means:

| Field | Meaning |
|---|---|
| `api` | Which of the three formats above the service uses |
| `base_url` | The service address. Must be `https://`, except `http://localhost` for local models |
| `api_key_env` | The **name** of the environment variable holding the key (the key itself goes in `.env`, never in config) |
| `auth` | How the key is sent: `bearer` (most services), `x-api-key`, `api-key` (Azure), or `none` (local servers) |
| `headers` | Extra non-secret headers, if the service needs any |
| `timeout_seconds` | How long to wait for one answer (default 600; reasoning models can be slow) |
| `max_tokens_field` | Chat format only: `max_tokens` or `max_completion_tokens`, whichever the service expects |
| `replay_fields` | Chat format only: extra fields of the AI's reply the service needs sent back. DeepSeek's thinking mode needs `["reasoning_content"]` (it returns an error otherwise). Default: none |

Good to know:

- The model must support tool calling (sometimes called function calling).
- **Same model for both roles works.** The reviewer still starts fresh, with its own copy
  of the code and no view of the coder's reasoning.
- **Different model families may reduce correlated errors**, since they may not share the
  same blind spots. TicketForge has not measured this yet.
- The planning step (assessor) uses the coder's model.
- Reusing a built-in name (for example `anthropic`) replaces it, such as to go through a company gateway.

Values are literal strings; existing environment variables take precedence. Keep secrets in
the ignored `.env`, never in example files or images. Needed permissions:

- Jira: browse issues, read/add comments, add remote links; be assignable to issues.
- GitHub: target contents and pull requests read/write; checks/statuses read.
- Model providers: API access for the models you configure. OpenAI Responses requests use
  `store: false`, so TicketForge replays the conversation rather than the provider keeping it.

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

### Running it for a team, on a server

TicketForge runs fine on your laptop for trying it out, but it only works while it is running.
For a team, run it on an always-on Linux server or VM:

- Install Docker and Python 3.9+ there, build the sandbox image, and keep the profile's state
  folder on a persistent disk (back it up; it holds the ticket history).
- Run `python3 -m ticketforge serve --config <profile>/config.json` under a process manager
  such as systemd, so it restarts after a crash or reboot. It picks up where it left off.
- **One repository = one TicketForge service = one Jira bot account.** Name each bot after
  its repository (for example "payments-bot"). Each service only picks up tickets assigned to
  its own bot, so services never step on each other.
- Add everyone who may answer the bot's questions to `allowed_jira_accounts`.
- Tip: add `AND statusCategory != Done` to `jira_jql` so marking a ticket Done also stops it.

**How it shares its time.** It works on one ticket at a time, like a single-core processor
that never interrupts a job. It stays on the current ticket, round after round, until that
ticket cannot continue: it is waiting for an answer, is blocked, has its PR waiting for your
review, is paused or unassigned, or is briefly backing off after a rate limit. Then it moves
to the ready ticket that has been waiting longest. A ticket that gets an answer goes to the
back of the queue; it never interrupts the current one. The current ticket is remembered
across restarts. While it works, it keeps reading Jira in the background (so no answer is
missed) and keeps checking waiting PRs.

Merged, closed and unassigned tickets are not read again. Reassigning a ticket to the bot
brings it back. There is no webhook server; Jira is polled.

## How the coder and reviewer talk

Each problem the reviewer finds is written down in a fixed shape: how serious it is
(`blocking` must be fixed, `advisory` is just a suggestion), the file and line, what is
wrong, and the proof. It gets an ID (`F1`, `F2`, …) and is kept in a list saved with the
ticket (the "finding ledger"), so nothing gets lost between rounds.

One round goes like this:

1. **The coder gets the open problems**, each with the reviewer's latest explanation. It
   cannot finish until it answers every ID: `fixed`, or `disputed` with proof (a test, a
   line from the ticket). If it skips one, it is told to try again.
2. **The reviewer looks at the new code fresh.** It is not shown the old problems yet, so
   it forms its own opinion first instead of just agreeing with what it said before.
3. **Then the reviewer settles the old problems**, in the same conversation. It now sees each
   old problem (and old advisory note) and the coder's answer (only the proof, not the coder's
   sales pitch), and marks each one `resolved`, `withdrawn` (the coder was right), or `open`
   (still wrong, and why). Problems it found in step 2 are numbered `N1`, `N2`, … and are
   **all kept** unless it explicitly drops one with a reason (for example "duplicate of F2");
   simply leaving one out does not make it disappear. Its final "pass" or "changes requested"
   must match what is still open, or it is told to correct itself.
4. **TicketForge decides what is next.** Anything blocking still open goes back to the coder.
   Advisory notes never cause another round; every note still in force, from any round, is
   listed in the pull request.
   When nothing blocking is left, the pull request is opened.
5. **If they disagree, you decide, straight away.** When the coder disputes a problem and
   the reviewer, after reading the coder's proof, still says it is wrong, the ticket pauses.
   Jira shows both sides' arguments. Reply with your decision; both AIs then look at that
   problem again with your answer in the ticket. There is no AI-versus-AI back-and-forth.
   (To allow more attempts before asking you, raise `max_finding_disputes`; default 1.)

`max_review_rounds` (default 3) caps the total number of rounds. Using two different models
may help, but neither AI replaces your review. Anything TicketForge tells an agent mid-run
(such as the old problems in step 3) is also pinned to its task, so trimming a long
conversation can never drop it.

## What the agents remember

Each AI run starts a fresh conversation, so TicketForge hands it what it needs instead of
making it rediscover the code every time:

| What | Who gets it | Where it comes from |
|---|---|---|
| **Package map**: every package, its one-line description, files, tests and internal imports | Coder and reviewer | `go list`, run once per starting commit and saved, so every ticket on that commit reuses it |
| **The coder's own notes** from its last round on this ticket (key files, decisions, what is left) | Coder only | The coder writes them at the end of each round |
| **Git history**: recent commits before the ticket, commits on the ticket, files changed so far | Coder only | Read from the ticket's branch |
| **Lessons**: facts about the repository from earlier tickets | Coder only | Proposed by the coder with the PR; kept only if you reply `/forge learn` and the PR is merged |
| **Build cache** | Each role its own | Shared across tickets of the repository, so builds are incremental |

**The reviewer stays independent.** It never sees the coder's notes, summary, reasoning or
lessons, and its copy of `PROJECT.md` leaves out the "Learned from tickets" section. It gets
the requirements, the code, your hand-written guide and the neutral package map. Commit
messages are kept neutral for the same reason (the reviewer can read them).

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
| `/forge learn` or `/forge learn 1,3` | Keep the coder's suggested repository facts (all, or the numbered ones); saved to `PROJECT.md` when the PR is merged |

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

The first command runs the normal tests. They use scripted stand-ins for the AIs, Jira and
GitHub, but real Git. They cover the whole flow: questions, changed requirements, the
coder/reviewer conversation and escalation to you, ticket assignment, restarts, retries,
and protected files.

The second command needs Docker. It builds a real Go sandbox and checks that Go builds and
tests actually run inside it, with no network and read-only Git. If you ask for it and
Docker is not running, it fails rather than quietly skipping.

## Current limits

Honest list of what this does **not** do yet:

- **Not proven on real work.** No live trial with real Jira, GitHub and models has been run,
  and there is no measured success rate on real tickets yet.
- **The AIs can still be wrong.** TicketForge forces the coder and reviewer to answer every
  point with evidence, but the quality of that evidence depends on the models. Services that
  copy the OpenAI format differ in how well they support tool calling.
- **Checks, not a baseline.** New profiles check formatting, build, tests, race tests and vet
  (skipping `vendor/`, `testdata/` and hidden folders). If your main branch already fails a
  check, every ticket will fail it too; there is no "it was already broken" comparison.
- **No automatic rebase.** The branch starts from `main` when the ticket starts. If `main`
  moves on and conflicts, you resolve it.
- **Memory is deliberately small.** The AI gets a package map, its own notes and the Git
  history, but no hidden long-term memory: anything kept across tickets is a lesson you
  approved in `PROJECT.md`. Long conversations are trimmed (big outputs are stored and can be
  re-read). There is no spending limit in dollars and no prompt caching yet.
- **Retries are careful, not magic.** Reads and AI calls are retried after short failures.
  Writes (comments, pull requests) are checked first instead of blindly repeated. Jira,
  GitHub and the local database cannot be updated as one unit, so in rare failures you might
  see a delayed or duplicate-looking status comment, never a duplicate pull request.
- **Docker is a strong fence, not a vault.** No network, no passwords, read-only system and
  Git files. It does not protect against a deliberately hostile repository or kernel bugs.
  Secrets committed in the repository are visible to the AI. Ticket and code text is sent to
  the AI service you configure.
- **Local data grows.** Workspaces, caches, the database, logs and traces stay on disk and
  contain private code. Clean them up yourself (stop the worker first).
- **Some things stay manual.** If someone else pushes to the agent's branch, it stops and
  waits for you. Merged or closed tickets do not reopen; create a new ticket. Jira
  attachments are not read.
- **Work in flight cannot be recalled.** An AI request already sent still finishes and is
  billed, even if you changed the ticket meanwhile; its result is then discarded.

See [architecture](docs/architecture.md), [review status](docs/release-readiness.md), and
[historical landscape/review](docs/landscape-and-review.md).
Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). License: [MIT](LICENSE).
