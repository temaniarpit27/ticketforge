# Contributing

TicketForge initially targets Jira Cloud, GitHub, and standalone Go modules.
Read the README limitations and docs/extending.md before proposing new integrations.

Keep changes focused and explain the problem, changed behavior, and verification.
Run the offline unittest suite. For sandbox/image changes, also run the opt-in real Docker
test from the README. Never replace a requested integration test with a mocked success.
Add meaningful regressions for lifecycle, permission, retry, or context-handling changes.

New adapters need contract tests and a controlled real-account pilot before being called
supported. Example templates intentionally raise NotImplementedError.

Do not submit credentials, private tickets, traces, profiles, personal interview notes,
or runtime databases. Use blank .env.example fields. Review the complete staged diff before
committing. Human maintainers review and merge changes; agent review does not authorize merging.

Contributions are under the project's MIT license.
