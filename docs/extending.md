# Extending TicketForge

The supported combination is **Jira Cloud + GitHub pull requests + a standalone Go module**.
GitHub Issues, GitLab, and other languages are extension targets, not implemented features.
The examples are intentionally incomplete and raise `NotImplementedError` instead of
pretending to support external services.

## Boundaries

`ticketforge/adapters.py` defines three structural contracts:

| Contract | Owns | Template |
|---|---|---|
| `IssueTracker` | Discovery, authorized snapshots/comments, deduplicated status messages, issue URL and links | `examples/adapters/issue_tracker_template.py` |
| `CodeHost` | Git transport credentials, normalized PR/MR state and checks/reviews | `examples/adapters/code_host_template.py` |
| `LanguageAdapter` | Manifest/context discovery, dependency image, verification commands | `examples/adapters/language_template.py` |

The coordinator receives tracker and code-host objects through dependency injection.
Existing `jira`/`github` attribute names are retained as compatibility aliases; integrations
must use the normalized contract rather than leaking native API shapes into the workflow.
Host Git fetch/push uses the selected code host's URL and credentials. Model tools never
receive those credentials.

An agent runner already has a separate `run(role, workspace, task, guard, trace)` interface.
The current implementation uses Anthropic; it can later be replaced independently of the
ticket lifecycle. No automatic host imports from repository files are supported.

## Adding a tracker, for example GitHub Issues

1. Implement `IssueTracker`, using the existing Jira adapter as a reference.
2. Map external identifiers to safe profile-local keys such as `GH-123`; the profile fixes
   owner/repository. Native URLs belong in metadata, not filenames. Different repositories
   need separate state directories and nonoverlapping filters.
3. Normalize snapshots to `{summary, description, notes}` and comments to `{id, author, text}`.
   IDs/authors must be stable strings. Exclude bot/unauthorized comments. Include edits and
   deletions in each complete snapshot. Keep command comments out of ordinary notes.
4. Preserve a bot-owned marker when reconciling outgoing comments. Handle pagination,
   rate limits, uncertain writes, and author permissions explicitly.
5. Register the trusted factory using `register_tracker("github_issues", Factory)` before
   creating integrations; set `issue_tracker` and provider-specific `tracker_options` in
   the local profile. The default CLI installs only built-ins. An extension's trusted launcher
   must register its factories before calling the CLI or coordinator; configuration strings
   do not import arbitrary modules.
6. Pass adapter contract tests and a controlled real-account test before calling it supported.

Keep token values in `.env`. Options should reference variable names such as `token_env`;
they should not contain the secret itself. See `examples/adapters/.env.example`.

## Adding GitLab merge requests

Implement `CodeHost` and register a factory with `register_code_host`. Normalize an MR to:

```text
number: integer provider-local identifier
html_url: native web URL
state: open or closed
head: {sha: full commit SHA}
merged: boolean
merge_commit_sha: SHA or null
mergeable: true, false, or null (unknown)
mergeable_state: clean only when checks, conflicts, and native policy allow merging
```

`ensure_pr` returns at least `number` and `html_url`. There is no merge method: humans
approve and merge in the code host. `checks` returns
names mapped to `success` or a non-success state. `reviews` uses stable numeric IDs, a
`user.login` identifier, `state` (`APPROVED`, `CHANGES_REQUESTED`, `DISMISSED`, or `COMMENTED`),
and optional `body`/`commit_id`. Translate semantics deliberately: do not map an unknown
native state to success. Test native approval/branch rules and lost responses.

The coordinator only observes merged/closed state. It never selects or performs a merge strategy.

## Adding another language

Create a tested language adapter and register it in `LANGUAGES`. Go profile generation is
currently explicit; add the new language to onboarding only after its environment works.
An adapter needs manifest/toolchain detection, dependency preparation, meaningful checks,
cache behavior, safe failure for unsupported layouts, and a real sandbox integration test.
Changing the prompt alone is insufficient. Avoid guessing when manifests are absent or
multiple language roots make the requested scope unclear.

## Repository guides and skills

Discovery reads bounded root guides: `README.md`, `AGENTS.md`, `AGENT.md`, `agent.md`,
`CLAUDE.md`, `TICKETFORGE.md`, contributing/architecture docs, plus the profile's `PROJECT.md`.
It catalogs up to 24 skills under `.agents/skills/*/SKILL.md` and
`.claude/skills/*/SKILL.md`. Names/descriptions/paths are supplied first; agents read relevant
skill bodies and local references using sandbox tools. Nested package guides must be read
before changing their files. See `examples/repository/` for reusable starting files.

Guide/skill text is task guidance, not authority to override system permissions, expose
secrets, import host plugins, change protected paths, or bypass review/approval. Symlinked
context files escaping the checkout are not loaded by host discovery. Arbitrary remote
imports referenced by an instruction file are not fetched automatically.

## Minimum contract checks for a new integration

Test duplicate/edited/deleted comments, unauthorized users, bot loops, pagination, unknown
remote states, unexpected PR head changes, new input during execution, restart, uncertain side
effects, and closure/merge outside the agent. Trackers must retain the ability to accept
human input during any active stage. A future provider must not silently weaken the policy.
