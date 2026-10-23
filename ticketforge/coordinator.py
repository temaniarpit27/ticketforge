from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

from . import review
from .adapters import language_adapter
from .project import REPO_MAP_FILE
from .runtime import LEARNED_HEADING, ExecutionError
from .store import FINISHED_STATES, Store, Superseded
from .integrations import RateLimited, RemoteError

log = logging.getLogger(__name__)


def save_lessons(path, key, lessons):
    """Append approved lessons to the project guide under one heading, one line each."""
    heading = LEARNED_HEADING
    text = path.read_text() if path.is_file() else ""
    if heading not in text:
        text = text.rstrip("\n") + ("\n\n" if text else "") + heading + "\n\n"
    elif not text.endswith("\n"):
        text += "\n"
    text += "".join(f"- {' '.join(lesson.split())} ({key})\n" for lesson in lessons)
    # Write to a temporary file first, so a crash never leaves a half-written guide.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


class Coordinator:
    """The "manager" that moves each ticket from start to pull request.

    The AIs only suggest: they edit files in a sandbox and hand back a structured result.
    This class, which is ordinary code, decides what happens next. It is the only part that
    runs the checks, commits, pushes, opens the pull request and writes to Jira.

    Two threads: one does the work (tick), one ticket at a time; the other keeps reading Jira
    for new comments and sends queued comments, so nothing is missed while the work runs.
    """

    def __init__(self, cfg, store: Store, jira, github, repo, agents, sandbox):
        self.cfg, self.store, self.jira, self.github = cfg, store, jira, github
        self.tracker, self.code_host = jira, github
        self.repo, self.agents, self.sandbox = repo, agents, sandbox
        self.stop = threading.Event()
        self.sync_lock = threading.RLock()
        self.post_lock = threading.Lock()
        # When each waiting PR is next checked: key -> ((version, head), monotonic deadline).
        self.status_schedule = {}

    def sync_one(self, key):
        # Serialize reads too, so an older API response cannot overwrite a newer snapshot.
        with self.sync_lock:
            snapshot, comments = self.jira.snapshot(key)
            self.store.sync(key, snapshot, comments)

    def poll(self):
        """Read Jira for every ticket the agent may still act on.

        That is: tickets the filter finds now (assigned to the bot), plus tickets still in
        progress even if the filter stopped matching them (that is how an unassignment is
        noticed). Merged, closed and unassigned tickets are never read again; a reassigned
        ticket shows up through the filter."""
        known = {t["key"]: t["state"] for t in self.store.all()}
        keys = {key for key in self.jira.discover() if known.get(key) not in ("merged", "closed")}
        keys.update(key for key, state in known.items() if state not in FINISHED_STATES)
        for key in sorted(keys):
            try:
                self.sync_one(key)
            except Exception:
                log.exception("Jira synchronization failed for %s", key)

    def flush(self):
        """Send queued Jira comments. Each is recorded in SQLite first (outbox), so a crash
        after sending is reconciled by the comment's marker instead of posting twice."""
        with self.post_lock:
            for item in self.store.pending_posts():
                self.jira.post_once(item["key"], item["id"], item["body"])
                self.store.mark_sent(item["id"])

    def post(self, key, identity, text):
        self.store.post(key, identity, text[:24000])

    def boundary(self, key, version):
        """Checkpoint between stages: pull fresh Jira input, then stop if the ticket moved on.

        Raises Superseded when new input, pause, unassignment or shutdown happened meanwhile,
        so stale model results are never committed or published."""
        self.sync_one(key)
        self.store.guard(key, version)
        if self.stop.is_set():
            raise Superseded("Service stopping")

    def guard(self, key, version):
        """Cheap local checkpoint (no Jira call), run between every agent tool call."""
        self.store.guard(key, version)
        if self.stop.is_set():
            raise Superseded("Service stopping")

    def trace(self, key, version, event):
        directory = Path(self.cfg.state_dir) / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{key}-v{version}.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": time.time(), **event}) + "\n")

    def result(self, key, version, role, result):
        """Handle an agent stopping for a human: questions go to Jira (waiting_input), a
        blocker is reported (blocked). Returns True when the run must stop here."""
        status = result["status"]
        if status not in ("needs_input", "blocked"):
            return False
        state = "waiting_input" if status == "needs_input" else "blocked"
        body = ("\n".join(f"{i + 1}. {q}" for i, q in enumerate(result["questions"]))
                if status == "needs_input" else result["summary"])
        self.store.update(key, version, state, last_result=result, notice=(f"{role}:{version}:{time.time_ns()}",
                  f"{role.capitalize()} {'needs clarification' if status == 'needs_input' else 'is blocked'}:\n\n{body}\n\n"
                  "Reply here. New requirements can be added at any time; /forge retry retries an infrastructure blocker."))
        return True

    def work(self, ticket):
        """One attempt for a ready ticket: assess, code, verify, commit, review, then decide.

        Every stage ends at a checkpoint. A return leaves the ticket in its next state
        (waiting_input, ready for another round, blocked, or awaiting_approval with a PR).
        """
        key, version = ticket["key"], ticket["version"]
        self.boundary(key, version)
        ticket = self.store.get(key)
        if ticket["data"].get("pr"):
            remote = self.github.pr(ticket["data"]["pr"])
            if remote.get("merged"):
                self.record_merge(key, remote.get("merge_commit_sha"), remote["html_url"])
                return
            if remote["state"] != "open":
                self.store.update(key, version, "closed", approval=None)
                self.post(key, f"closed:{remote['number']}", "The PR is closed. Create a follow-up ticket for further changes.")
                return
        # A PR that a human already merged or closed ends the run; never code on top of it.
        # review_round bounds coder/reviewer exchanges; round counts every attempt for messages.
        review_round = ticket["data"].get("review_round", 0)
        round_number = ticket["data"].get("round", 0) + 1
        if review_round >= self.cfg.max_review_rounds:
            self.store.update(key, version, "blocked", notice=(f"round-limit:{version}", "Coding/review limit reached. Please inspect the findings. Add guidance here or use /forge retry after resolving the blocker."))
            return
        self.store.update(key, version, "assessing", round=round_number, approval=None, notice=(f"started:{version}:{round_number}",
                  f"Started work on specification v{version}, attempt {round_number}. Investigating and implementing in an isolated workspace."))
        self.flush()
        workspace, branch = self.repo.prepare(key)
        project_context = self.repo.context(workspace)
        base = self.repo.git("rev-parse", "refs/ticketforge/base", cwd=workspace)
        repo_map = self.repo_map(workspace, base, project_context)
        if repo_map:
            project_context["repo_map"] = repo_map
        self.store.update(key, version, project_context=project_context)
        self.post(key, f"context:{version}", f"Repository discovered: {project_context['language']}. " +
                  "Read the manifest, project structure, and available guides. Relevant source and tests will be inspected before changes.\n" +
                  "\n".join(project_context.get("warnings", [])))
        guard = lambda: self.guard(key, version)
        tracer = lambda event: self.trace(key, version, event)
        ledger = ticket["data"].get("findings") or review.new_ledger()
        if ticket["data"].get("ledger_version") != version:
            # The human answered an escalation (or added input): disputed findings get a fresh hearing.
            review.reopen_escalated(ledger, version)
            self.store.update(key, version, findings=ledger, ledger_version=version)
        open_ids = [item["id"] for item in review.open_findings(ledger)]
        # The coder sees the spec, project facts, failed checks from its last attempt and every
        # open finding with the reviewer's latest reasoning. Never the reviewer's checkout.
        task = {"ticket": key, "specification_version": version, "specification": ticket["snapshot"],
                "project_context": project_context,
                "failed_checks": ticket["data"].get("failed_checks", []),
                "open_findings": review.for_coder(ledger),
                "verification_commands": self.cfg.verification_commands}
        # Requirements changed since the code in the workspace was written: show the agents the
        # spec that code was written for, so they can see what is new, changed or dropped.
        changed = self.changed_requirements(ticket)
        if changed:
            task["previous_specification"] = changed
        # What has been done so far (commits, changed files) so the coder builds on it.
        task["history"] = self.repo.history(workspace)
        # The coder's own notes from its last round of this ticket, so it does not have to
        # rediscover the code from scratch. The reviewer never sees them.
        if ticket["data"].get("coder_notes"):
            task["your_notes_from_last_round"] = ticket["data"]["coder_notes"]
        # Read-only planning once per specification version: cheap to correct, and questions
        # surface before any code exists. Later rounds of the same version reuse the plan.
        if ticket["data"].get("assessed_version") != version:
            assessment = self.agents.run("assessor", workspace, task, guard, tracer)
            self.boundary(key, version)
            if self.result(key, version, "assessment", assessment):
                return
            self.store.update(key, version, assessment=assessment, assessed_version=version,
                              notice=(f"assessment:{version}", "Read-only assessment complete. Plan and assumptions:\n\n" +
                                      assessment["summary"] + "\n\nYou can correct these on this ticket at any time."))
        else:
            assessment = ticket["data"].get("assessment")
        task["assessment"] = assessment
        # From here the workspace may hold work towards this version of the requirements.
        self.store.update(key, version, "coding", worked_specification={"version": version, "snapshot": ticket["snapshot"]})

        # Rejecting here sends the error back to the model as a tool error, so it must retry
        # with one response per open finding instead of silently skipping one.
        def coder_finished(result):
            if result["status"] == "ready":
                review.require_ids(result["responses"], open_ids, "responses (one per open finding: fixed or disputed)")
            return None

        coded = self.agents.run("coder", workspace, task, guard, tracer, on_finish=coder_finished)
        self.boundary(key, version)
        # Kept even if the coder stops to ask a question: the next round starts from them.
        self.store.update(key, version, coder_notes={"version": version, "round": round_number, "text": coded["notes"]})
        if self.result(key, version, "coder", coded):
            return
        # Save the coder's "fixed"/"disputed" answers now, so the reviewer still sees them
        # later even if this round's checks fail and the coder has to try again.
        review.record_coder(ledger, coded["responses"], round_number)
        self.store.update(key, version, findings=ledger)
        # The coordinator runs the checks itself; a model saying "tests pass" is not evidence.
        verification = []
        for command in self.cfg.verification_commands:
            guard()
            value = self.sandbox.run(workspace, command, guard, role="verify")
            verification.append({"command": command, **value})
            tracer({"role": "coordinator", "verification": verification[-1]})
        self.boundary(key, version)
        failed = [v for v in verification if v["exit_code"] != 0]
        if failed:
            repairs = ticket["data"].get("verification_repairs", 0) + 1
            target = "blocked" if repairs > self.cfg.max_verification_repairs else "ready"
            self.store.update(key, version, target, failed_checks=failed, verification=verification, verification_repairs=repairs,
                              notice=(f"checks-failed:{version}:{round_number}", "Verification failed. Returning evidence to the coder for repair:\n" +
                      "\n".join(f"{v['command']}: exit {v['exit_code']}\n{v['output'][:2500]}" for v in failed) +
                      ("\nVerification repair budget exhausted; add guidance or /forge retry." if target == "blocked" else "")))
            return
        # Only the coordinator commits (protected paths are refused inside commit()).
        head, base = self.repo.commit(workspace, key, version)
        self.boundary(key, version)
        disputed = [r["id"] for r in coded["responses"] if r["action"] == "disputed"]
        self.store.update(key, version, "reviewing", head=head, base=base, branch=branch, failed_checks=[],
                          verification=verification, coder_result=coded, review_round=review_round + 1, notice=(f"review-start:{version}:{head}:{round_number}",
                          f"Implementation checks passed. Independent review started for commit {head} against specification v{version}." +
                          (f"\nCoder responded to {len(coded['responses'])} open finding(s); disputed: {', '.join(disputed) or 'none'}." if coded["responses"] else "")))
        self.flush()
        reviewed = self.review(key, version, workspace, ticket, head, base, ledger, guard, tracer)
        if reviewed is None:
            return
        # The ledger decides the outcome, not the reviewer's own status word: escalate repeated
        # disagreement to the human, send open blocking findings back, otherwise publish.
        open_items, escalated, advisories = review.apply_review(ledger, reviewed, review_round + 1, self.cfg.max_finding_disputes)
        self.store.update(key, version, findings=ledger, review=reviewed,
                          reviewed_specification={"version": version, "snapshot": ticket["snapshot"]})
        if escalated:
            self.store.update(key, version, "waiting_input", notice=(f"escalated:{version}:{head}",
                "The coder and reviewer disagree and need your decision:\n\n" +
                "\n\n".join(review.describe(item) for item in escalated) +
                "\n\nReply on this ticket with your decision for each finding (for example: \"" + escalated[0]["id"] +
                ": the reviewer is right, fix it\" or \"" + escalated[0]["id"] + ": keep the current behaviour\")."))
            return
        if open_items:
            self.store.update(key, version, "ready", notice=(f"findings:{version}:{head}:{round_number}",
                "Independent reviewer requested changes:\n\n" + "\n\n".join(review.describe(item) for item in open_items) +
                "\n\nReturning these findings to the coder to fix or dispute with evidence."))
            return
        # Only coordinator commits in coder workspace. Never accept reviewer experiments as implementation.
        if self.repo.git("rev-parse", "HEAD", cwd=workspace) != head:
            raise RuntimeError("Implementation commit changed during review")
        self.store.update(key, version, "publishing", reviewed_version=version, reviewed_head=head, advisories=advisories)
        self.boundary(key, version)
        self.repo.publish(workspace, branch)
        self.boundary(key, version)
        settled = [i for i in ledger["items"].values() if i["status"] in ("resolved", "withdrawn")]
        body = (f"Implements {key}, specification v{version}.\n\nTicket: {self.tracker.issue_url(key)}\n\n"
                f"Commit: {head}\n\nSummary:\n{coded['summary']}\n\n"
                "Verification run by coordinator:\n" + "\n".join(f"- `{v['command']}`: exit {v['exit_code']}" for v in verification) +
                f"\n\nIndependent review ({self.cfg.reviewer_provider}/{self.cfg.reviewer_model}):\n{reviewed['summary']}" +
                ("\n\nFindings settled in coder/reviewer dialogue:\n" + "\n".join(f"- {i['id']} {i['status']}: {i['problem']}" for i in settled) if settled else "") +
                ("\n\nAdvisory notes (not blocking):\n" + "\n".join(f"- {review.describe(i)}" for i in advisories) if advisories else "") +
                "\n\nDiscuss implementation changes on Jira. Human approval and merging take place on GitHub.")
        pr = self.github.ensure_pr(branch, f"{key}: {ticket['snapshot']['summary']}"[:200], body)
        self.store.update(key, version, "awaiting_approval", pr=pr["number"], pr_url=pr["html_url"], approval=None)
        self.announce_pr(key, version)

    def review(self, key, version, workspace, ticket, head, base, ledger, guard, tracer):
        """Run the reviewer: first a fresh look, then (same conversation) settle old findings.

        Returns the reviewer's final result, or None if it stopped to ask a question or
        hit a blocker (already reported on Jira)."""
        prior = review.for_reconcile(ledger)
        prior_ids = [item["id"] for item in prior]
        phase = {"reconciling": False}

        # Called each time the reviewer calls finish. The first call ends the independent pass;
        # returning the old findings keeps the SAME conversation going to settle them, so the
        # reviewer cannot see earlier findings before it has formed its own opinion.
        def reviewer_finished(result):
            if result["status"] in ("needs_input", "blocked"):
                return None
            if not phase["reconciling"]:
                if result["resolutions"]:
                    raise ValueError("The first pass is independent: leave resolutions empty; prior findings come next")
                if not prior:
                    return None
                phase["reconciling"] = True
                return {"phase": "reconcile", "instructions": review.RECONCILE_INSTRUCTIONS,
                        "prior_findings": prior, "your_independent_findings": result["findings"]}
            review.require_ids(result["resolutions"], prior_ids, "resolutions (one per prior finding)")
            return None

        reviewer_workspace = self.repo.review_workspace(key, workspace, head, version)
        review_context = self.repo.context(reviewer_workspace, reviewer=True)
        repo_map = self.repo_map(reviewer_workspace, base, review_context)
        if repo_map:
            review_context["repo_map"] = repo_map
        review_task = {"ticket": key, "specification_version": version, "specification": ticket["snapshot"],
                       "project_context": review_context,
                       "base_sha": base, "head_sha": head, "verification_commands": self.cfg.verification_commands}
        # Earlier findings may have been raised against older requirements; the reviewer needs
        # to know what changed to withdraw ones that no longer apply.
        changed = ticket["data"].get("reviewed_specification")
        if changed and changed["version"] != version and changed["snapshot"] != ticket["snapshot"]:
            review_task["previous_specification"] = changed
        reviewed = self.agents.run("reviewer", reviewer_workspace, review_task, guard, tracer, on_finish=reviewer_finished)
        self.boundary(key, version)
        if self.result(key, version, "reviewer", reviewed):
            return None
        return reviewed

    def repo_map(self, workspace, base, project_context):
        """A package map of the repository at the ticket's starting commit.

        Made once per commit (by running `go list` in the sandbox) and saved, so every ticket
        that starts from the same commit of the main branch gets it instantly. It saves the
        agents from rediscovering the package layout with grep on every ticket and round.
        Never blocks work: if it cannot be made, the agents simply explore as before.
        """
        # Only when discovery confirmed the configured language, and for a real commit.
        if project_context.get("language") != self.cfg.language or not re.fullmatch(r"[0-9a-f]{40}", base or ""):
            return None
        saved = Path(self.cfg.state_dir) / "repo-maps" / f"{base}.json"
        if saved.is_file():
            repo_map = json.loads(saved.read_text())
            return None if repo_map.get("unavailable") else repo_map
        repo_map = {"unavailable": True}
        try:
            adapter = language_adapter(self.cfg.language)
            result = self.sandbox.run(workspace, adapter.repo_map_command(), role="verify", read_only=True)
            output = self.sandbox.cache_dir(workspace, "verify") / REPO_MAP_FILE
            if result["exit_code"] == 0 and output.is_file() and output.stat().st_size <= 20 * 1024 * 1024:
                repo_map = adapter.parse_repo_map(output.read_text(errors="replace"), project_context.get("module", ""))
                repo_map["commit"] = base
            if output.is_file():
                output.unlink()
        except (ValueError, OSError, ExecutionError):
            log.warning("Repository map unavailable for %s; agents will explore without it", base)
        # A failure is remembered too, so the same commit is not retried every round.
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(repo_map))
        return None if repo_map.get("unavailable") else repo_map

    @staticmethod
    def changed_requirements(ticket):
        """The spec the workspace was last coded against, if the requirements changed since."""
        worked = ticket["data"].get("worked_specification")
        if worked and worked["version"] != ticket["version"] and worked["snapshot"] != ticket["snapshot"]:
            return worked
        return None

    def announce_pr(self, key, version):
        """Post the PR link on Jira and link it once. Safe to repeat: the post is deduplicated
        by identity and the link is recorded, which also recovers a crash after PR creation."""
        ticket = self.store.get(key)
        data, head = ticket["data"], ticket["data"]["head"]
        url = data["pr_url"]
        lessons = data["coder_result"].get("lessons") or []
        proposal = ("\n\nThe coder suggests remembering these facts about the repository for future tickets:\n" +
                    "\n".join(f"{i + 1}. {lesson}" for i, lesson in enumerate(lessons)) +
                    "\nReply /forge learn to keep all of them, or for example /forge learn 1,3 for some. "
                    "They are added to the project guide (PROJECT.md) only after this PR is merged.") if lessons else ""
        self.post(key, f"pr:{version}:{head}",
                  f"PR raised: {url}\n\nIndependent review completed; configured local checks passed.\n"
                  f"Specification: v{version}\nCommit: {head}\n\n{data['coder_result']['summary']}\n\n"
                  "Please review and merge manually on GitHub. Request implementation changes on this ticket.\n\n"
                  "New input requires fresh verification and agent review. TicketForge never approves or merges your PR." +
                  proposal)
        if data.get("jira_linked") != url:
            self.jira.link(key, url)
            self.store.update(key, version, jira_linked=url)

    def record_merge(self, key, sha, url):
        """Mark the ticket merged; then save any lessons you approved into the project guide.

        Lessons are only kept for merged work, and only the ones a human approved on Jira,
        because whatever is in the guide is read by every future agent run."""
        self.store.merged(key, sha, url)
        data = self.store.get(key)["data"]
        approved = data.get("lessons_approved") or []
        if not approved or data.get("lessons_saved"):
            return
        if not self.cfg.project_guide:
            self.post(key, "lessons", "Approved notes were not saved: set project_guide in the profile to keep them.")
            return
        save_lessons(Path(self.cfg.project_guide), key, approved)
        self.store.annotate(key, lessons_saved=True)
        self.post(key, "lessons", f"Saved {len(approved)} note(s) to the project guide; future tickets will read them.")

    def check_merge(self, ticket):
        """Observe a waiting PR: report checks/reviews on Jira and detect the human's merge or
        close. TicketForge never merges; a PR head changed by someone else blocks the ticket."""
        key, version, data = ticket["key"], ticket["version"], ticket["data"]
        self.boundary(key, version)
        self.announce_pr(key, version)
        pr = self.github.pr(data["pr"])
        if pr.get("merged"):
            self.record_merge(key, pr.get("merge_commit_sha"), pr["html_url"])
            return
        if pr["state"] != "open":
            self.store.update(key, version, "closed", approval=None)
            self.post(key, f"closed:{pr['number']}", "PR was closed without merging. This run is closed; use a new ticket for follow-up work.")
            return
        head = pr["head"]["sha"]
        if head != data["head"]:
            self.store.update(key, version, "blocked", approval=None)
            self.post(key, f"unexpected-head:{version}:{head}", "The PR branch changed outside TicketForge. The agent review applies to the previous commit; reconcile the branch before retrying.")
            return
        checks = self.github.checks(head)
        reviews = self.github.reviews(pr["number"])
        # Mirror review state into Jira; product feedback belongs in Jira comments.
        summary = {"checks": checks, "reviews": [
            {"author": r.get("user", {}).get("login"), "state": r.get("state"),
             "body": r.get("body", "")[:2000], "commit": r.get("commit_id")} for r in reviews]}
        digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()[:16]
        if digest != data.get("github_digest"):
            self.store.update(key, version, github_digest=digest)
            self.post(key, f"github:{head}:{digest}", "PR status update:\n" +
                      "\n".join(f"{name}: {value}" for name, value in checks.items()) +
                      ("\nNo checks reported yet." if not checks else "") +
                      "\n\nGitHub reviews:\n" + "\n".join(f"{r['author']}: {r['state']}\n{r['body']}" for r in summary["reviews"]) +
                      "\n\nPlease write implementation requests on this Jira ticket.")

    def tick(self):
        """One scheduler pass. Works like a single-core processor that never interrupts a job:

        1. Check PRs waiting for your review, each on its own (slower) schedule.
        2. Keep working on the current ticket for as long as it can continue. Only when it
           cannot (waiting for an answer, blocked, PR raised, paused, unassigned, or backing
           off after a rate limit) switch to the ready ticket that has waited longest.

        Errors are handled per ticket, so one failing ticket never stops the others.
        """
        for ticket in self.store.all():
            if self.stop.is_set():
                return
            if ticket["state"] == "awaiting_approval" and self.pr_check_due(ticket):
                self.attempt(ticket, self.check_merge)
        if self.stop.is_set():
            return
        chosen = self.next_ticket(self.store.all())
        if chosen:
            # Remember the choice, so after a restart the worker continues the same ticket.
            self.store.set_current(chosen["key"])
            self.attempt(chosen, self.work)

    def next_ticket(self, tickets):
        """Pick what to work on: the current ticket while it is ready, otherwise the ready
        ticket that has been waiting longest (ties: lowest key). None when nothing is ready."""
        now = time.time()
        ready = [t for t in tickets if t["state"] == "ready" and t["data"].get("retry_at", 0) <= now]
        current = self.store.current()
        for ticket in ready:
            if ticket["key"] == current:
                return ticket
        return min(ready, key=lambda t: (t["ready_since"] or 0, t["key"]), default=None)

    def pr_check_due(self, ticket):
        """True when a waiting PR should be checked again. A new spec or commit checks at once."""
        token = (ticket["version"], ticket["data"].get("head"))
        previous = self.status_schedule.get(ticket["key"])
        now = time.monotonic()
        if previous is not None and previous[0] == token and now < previous[1]:
            return False
        # Schedule before the check, so a failing check cannot turn into a tight retry loop.
        self.status_schedule[ticket["key"]] = (token, now + self.cfg.status_poll_seconds)
        return True

    def attempt(self, ticket, step):
        """Run one step (work or check_merge) for a ticket and turn failures into ticket state.

        Network failures defer the ticket (rate limits honour Retry-After) and block it only
        after max_remote_retries failures in a row; any other error blocks it with the reason.
        """
        key, version = ticket["key"], ticket["version"]
        try:
            step(ticket)
            self.store.remote_success(key, version)
        except Superseded:
            log.info("Task %s superseded or stopped", key)
        except RemoteError as exc:
            try:
                retries = self.store.get(key)["data"].get("remote_retries", 0) + 1
                delay = (exc.retry_after * 2 ** (retries - 1)
                         if isinstance(exc, RateLimited) else self.cfg.status_poll_seconds)
                target = "awaiting_approval" if ticket["state"] == "awaiting_approval" else "ready"
                if retries > self.cfg.max_remote_retries:
                    target = "blocked"
                action = "Retry budget exhausted; resolve the blocker and comment /forge retry." if target == "blocked" else "Retry scheduled."
                self.store.update(key, version, target, retry_at=time.time() + delay, remote_retries=retries,
                                  notice=(f"deferred:{version}:{time.time_ns()}", f"Remote request deferred: {exc}. Saved work is retained. {action}"))
            except Superseded:
                pass
        except Exception as exc:
            log.exception("Task %s failed", key)
            try:
                self.store.update(key, version, "blocked", error=str(exc)[:2000], notice=(f"error:{version}:{time.time_ns()}",
                          f"Work stopped: {type(exc).__name__}: {str(exc)[:1500]}\nSaved work is retained. Resolve the blocker and comment /forge retry."))
            except Superseded:
                pass

    def run(self):
        """Foreground service loop until stop is set (Ctrl+C / SIGTERM)."""
        self.store.recover()
        # Read Jira once before resuming any saved work, so it starts from the latest input.
        self.poll()
        def background():
            next_poll = time.monotonic() + self.cfg.poll_seconds
            failures = 0
            while not self.stop.wait(1):
                try:
                    if time.monotonic() >= next_poll:
                        self.poll()
                        next_poll = time.monotonic() + self.cfg.poll_seconds
                    self.flush()
                    failures = 0
                except Exception:
                    log.exception("Background integration failed; durable messages remain queued")
                    failures += 1
                    delay = min(60, 2 ** min(failures, 6))
                    next_poll = time.monotonic() + delay
                    self.stop.wait(delay)
        thread = threading.Thread(target=background, name="jira-events", daemon=True)
        thread.start()
        try:
            while not self.stop.is_set():
                self.tick()
                self.stop.wait(1)
        finally:
            self.stop.set()
            thread.join(timeout=95)
