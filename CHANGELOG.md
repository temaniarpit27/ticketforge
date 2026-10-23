# Changelog

## 0.3.0 — coder/reviewer dialogue

- Structured findings (blocking/advisory) with stable IDs in a persistent ledger.
- Coder must answer every open finding as fixed or disputed with evidence.
- Reviewer reviews independently, then reconciles prior findings in the same session.
- Findings disputed past `max_finding_disputes` escalate to Jira with both sides' evidence.
- Advisory findings no longer block; they are listed in the PR description.
- Configurable model providers per role: Anthropic Messages, OpenAI Responses (stateless), or any
  OpenAI-compatible Chat Completions endpoint, including local servers. Coder and reviewer may share one.
- Model call timeout is configurable per provider (default 600s, was a fixed 90s).
- Only tickets assigned to the agent's Jira account are picked up; unassigning stops work.
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
