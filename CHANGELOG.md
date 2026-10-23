# Changelog

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
