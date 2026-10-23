---
name: go-change
description: Implement a focused Go change using repository conventions and meaningful checks.
---

Inspect relevant packages and tests. Preserve API compatibility and error/cancellation
semantics. Run gofmt on changed Go files and the repository's configured checks. Where
relevant, inspect goroutine lifetimes, shared state, context propagation, and error wrapping.
Avoid mechanical changes unrelated to the requirement. If a check needs unavailable
dependencies or services, report that evidence and ask for the required environment.
