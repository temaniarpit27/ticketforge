---
name: clarify-ticket
description: Resolve material requirement ambiguity from Jira and repository evidence, at any stage.
---

Inspect the current specification, authorized human notes, and relevant existing code.
Start with the discovered project_context. Read applicable AGENTS.md and TICKETFORGE.md,
then inspect actual entry points and relevant packages/tests. A directory name or README
does not prove runtime behavior. If documentation is absent, investigate source first.
Ask on Jira when missing product or architectural decisions materially affect the solution.
Separate explicit requirements, evidence-backed implementation decisions, and assumptions.
Ask a small batch of precise questions only when answers materially affect correctness,
scope, security, or acceptance criteria. Investigate code before asking questions it answers.
You may ask questions during implementation; do not silently invent product decisions.
Return needs_input with questions when blocked. The coordinator posts them to Jira and
resumes you after a human response. Do not directly contact Jira or other services.
The newest specification supersedes conflicting old assumptions. Preserve useful work,
but inspect the actual workspace rather than trusting a previous summary.
