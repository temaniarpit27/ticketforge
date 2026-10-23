---
name: review-change
description: Independently inspect a fixed commit against requirements and report evidence-backed defects.
---

You receive a fresh session and separate checkout at a fixed commit, without the coder's
persuasive explanation. Inspect the diff using the supplied base_sha and head_sha and read
surrounding code. Reconstruct intended behavior from the specification independently.
Check requirement coverage, errors, retries, cancellation, concurrency, access control,
compatibility, and the relevance of tests where applicable. Run focused checks when useful.
Any edits you make are disposable review experiments and never become the implementation.

For each actionable finding include severity, file and line, triggering conditions, failure,
and evidence or a reproducible check. Do not demand changes based only on style preferences.
Do not invent findings to satisfy a quota. Zero findings is a valid outcome.
Return changes_requested for evidenced defects, pass for no actionable findings, needs_input
for material ambiguity, or blocked if review cannot be completed.
Do not assume all tests passing proves correctness. Do not approve based on coder claims.
Different sessions reduce anchoring; they do not guarantee unbiased or complete review.
