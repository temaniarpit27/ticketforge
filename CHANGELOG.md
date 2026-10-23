# Changelog

## 0.3.0 — coder/reviewer dialogue

- Structured findings (blocking/advisory) with stable IDs in a persistent ledger.
- Coder must answer every open finding as fixed or disputed with evidence.
- Reviewer reviews independently, then reconciles prior findings in the same session.
- A finding the reviewer still insists on after the coder disputes it escalates to Jira with
  both sides' evidence (`max_finding_disputes`, default 1: on the first disagreement).
- Advisory findings no longer block; they are listed in the PR description.
- Configurable model providers per role: Anthropic Messages, OpenAI Responses (stateless), or any
  OpenAI-compatible Chat Completions endpoint, including local servers. Coder and reviewer may share one.
- Model call timeout is configurable per provider (default 600s, was a fixed 90s).
- Only tickets assigned to the agent's Jira account are picked up; unassigning stops work.
- External review fixes: first-pass reviewer findings are kept unless explicitly dropped; the
  reviewer's verdict is checked against the combined result; advisory notes from every round
  reach the PR; coordinator replies are pinned against history trimming; PR checks honour
  back-off; unassigned tickets still have their open PR watched; lesson saving is retried and
  repeat-safe; `replay_fields` lets chat providers such as DeepSeek get required fields back.
- Less rediscovery: a `go list` package map saved per starting commit; the coder's own notes
  and the ticket's Git history passed to its next round; build caches shared across tickets
  (per role). The coder may propose repository facts; `/forge learn` keeps them, and they are
  appended to the profile's `PROJECT.md` only when the PR is merged.
- The reviewer never receives coder-written text, including approved lessons.
- One-ticket-at-a-time scheduling: the worker stays on its current ticket until it cannot
  continue, then takes the ready ticket that waited longest; the current ticket survives
  restarts. Merged, closed and unassigned tickets are no longer read from Jira on every poll.
  Waiting PRs are checked every 120 s by default (was 30 s).
- Requirement changes: `jira_spec_fields` adds custom Jira fields (such as acceptance criteria)
  to the requirements, and after a change both agents see the previous specification beside
  the current one so obsolete work and findings can be removed.

## 0.2.0 — prototype

- Read-only assessment, separate verification/review repair limits, and human-only PR handoff.
- Go sandbox PATH, binary-size, writable cache, and role-isolation fixes with a real Docker test.
- Scheduled PR polling, bounded transient retries, and recoverable bounded model history.
- Nested environment-file protection and explicit non-actionable Jira notes.
- Tracker/code-host/language extension contracts and repository guide/skill templates.
- Public setup, limitations, review status, and MIT license.
- Follow-up review: reset consecutive remote failures on success, reuse reviewer caches
  per ticket/image, add gofmt/build defaults, and defer GitHub secondary rate limits.
- Exclude vendor, testdata and hidden paths from the formatting gate; exercise exclusions
  in the real Docker test while retaining failures for invalid/unformatted project source.

This prototype has passed local runtime tests and external code review. It is not a
live-account pilot result.
