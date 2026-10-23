from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from .store import Store, Superseded
from .integrations import RateLimited, RemoteError

log = logging.getLogger(__name__)


class Coordinator:
    def __init__(self, cfg, store: Store, jira, github, repo, agents, sandbox):
        self.cfg, self.store, self.jira, self.github = cfg, store, jira, github
        self.tracker, self.code_host = jira, github
        self.repo, self.agents, self.sandbox = repo, agents, sandbox
        self.stop = threading.Event()
        self.sync_lock = threading.RLock()
        self.post_lock = threading.Lock()

    def sync_one(self, key):
        # Serialize reads too, so an older API response cannot overwrite a newer snapshot.
        with self.sync_lock:
            snapshot, comments = self.jira.snapshot(key)
            self.store.sync(key, snapshot, comments)

    def poll(self):
        keys = set(self.jira.discover())
        # Continue monitoring known tickets even when the discovery filter no longer matches.
        keys.update(t["key"] for t in self.store.all())
        for key in sorted(keys):
            try:
                self.sync_one(key)
            except Exception:
                log.exception("Jira synchronization failed for %s", key)

    def flush(self):
        with self.post_lock:
            for item in self.store.pending_posts():
                self.jira.post_once(item["key"], item["id"], item["body"])
                self.store.mark_sent(item["id"])

    def post(self, key, identity, text):
        self.store.post(key, identity, text[:24000])

    def boundary(self, key, version):
        self.sync_one(key)
        self.store.guard(key, version)
        if self.stop.is_set():
            raise Superseded("Service stopping")

    def guard(self, key, version):
        self.store.guard(key, version)
        if self.stop.is_set():
            raise Superseded("Service stopping")

    def trace(self, key, version, event):
        directory = Path(self.cfg.state_dir) / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{key}-v{version}.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": time.time(), **event}) + "\n")

    def result(self, key, version, role, result):
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
        key, version = ticket["key"], ticket["version"]
        self.boundary(key, version)
        ticket = self.store.get(key)
        if ticket["data"].get("pr"):
            remote = self.github.pr(ticket["data"]["pr"])
            if remote.get("merged"):
                self.store.merged(key, remote.get("merge_commit_sha"), remote["html_url"])
                return
            if remote["state"] != "open":
                self.store.update(key, version, "closed", approval=None)
                self.post(key, f"closed:{remote['number']}", "The PR is closed. Create a follow-up ticket for further changes.")
                return
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
        self.store.update(key, version, project_context=project_context)
        self.post(key, f"context:{version}", f"Repository discovered: {project_context['language']}. " +
                  "Read the manifest, project structure, and available guides. Relevant source and tests will be inspected before changes.\n" +
                  "\n".join(project_context.get("warnings", [])))
        guard = lambda: self.guard(key, version)
        tracer = lambda event: self.trace(key, version, event)
        task = {"ticket": key, "specification_version": version, "specification": ticket["snapshot"],
                "project_context": project_context,
                "review_findings_or_failed_checks": ticket["data"].get("feedback", []),
                "verification_commands": self.cfg.verification_commands}
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
        self.store.update(key, version, "coding")
        coded = self.agents.run("coder", workspace, task, guard, tracer)
        self.boundary(key, version)
        if self.result(key, version, "coder", coded):
            return
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
            self.store.update(key, version, target, feedback=failed, verification=verification, verification_repairs=repairs,
                              notice=(f"checks-failed:{version}:{round_number}", "Verification failed. Returning evidence to the coder for repair:\n" +
                      "\n".join(f"{v['command']}: exit {v['exit_code']}\n{v['output'][:2500]}" for v in failed) +
                      ("\nVerification repair budget exhausted; add guidance or /forge retry." if target == "blocked" else "")))
            return
        head, base = self.repo.commit(workspace, key, version)
        self.boundary(key, version)
        self.store.update(key, version, "reviewing", head=head, base=base, branch=branch,
                          verification=verification, coder_result=coded, review_round=review_round + 1, notice=(f"review-start:{version}:{head}:{round_number}",
                          f"Implementation checks passed. Independent review started for commit {head} against specification v{version}."))
        self.flush()
        reviewer_workspace = self.repo.review_workspace(key, workspace, head, version)
        review_task = {"ticket": key, "specification_version": version, "specification": ticket["snapshot"],
                       "project_context": self.repo.context(reviewer_workspace),
                       "base_sha": base, "head_sha": head, "verification_commands": self.cfg.verification_commands}
        reviewed = self.agents.run("reviewer", reviewer_workspace, review_task, guard, tracer)
        self.boundary(key, version)
        if self.result(key, version, "reviewer", reviewed):
            return
        if reviewed["status"] == "changes_requested":
            self.store.update(key, version, "ready", feedback=reviewed["findings"], review=reviewed,
                              notice=(f"findings:{version}:{head}:{round_number}", "Independent reviewer requested changes:\n\n" +
                      "\n\n".join(reviewed["findings"]) + "\n\nReturning these findings to the coder."))
            return
        # Only coordinator commits in coder workspace. Never accept reviewer experiments as implementation.
        if self.repo.git("rev-parse", "HEAD", cwd=workspace) != head:
            raise RuntimeError("Implementation commit changed during review")
        self.store.update(key, version, "publishing", review=reviewed,
                          reviewed_version=version, reviewed_head=head, feedback=[])
        self.boundary(key, version)
        self.repo.publish(workspace, branch)
        self.boundary(key, version)
        body = (f"Implements {key}, specification v{version}.\n\nTicket: {self.tracker.issue_url(key)}\n\n"
                f"Commit: {head}\n\nSummary:\n{coded['summary']}\n\n"
                "Verification run by coordinator:\n" + "\n".join(f"- `{v['command']}`: exit {v['exit_code']}" for v in verification) +
                f"\n\nIndependent review:\n{reviewed['summary']}\n\nDiscuss implementation changes on Jira. Human approval and merging take place on GitHub.")
        pr = self.github.ensure_pr(branch, f"{key}: {ticket['snapshot']['summary']}"[:200], body)
        self.store.update(key, version, "awaiting_approval", pr=pr["number"], pr_url=pr["html_url"], approval=None)
        self.announce_pr(key, version)

    def announce_pr(self, key, version):
        ticket = self.store.get(key)
        data, head = ticket["data"], ticket["data"]["head"]
        url = data["pr_url"]
        self.post(key, f"pr:{version}:{head}",
                  f"PR raised: {url}\n\nIndependent review completed; configured local checks passed.\n"
                  f"Specification: v{version}\nCommit: {head}\n\n{data['coder_result']['summary']}\n\n"
                  "Please review and merge manually on GitHub. Request implementation changes on this ticket.\n\n"
                  "New input requires fresh verification and agent review. TicketForge never approves or merges your PR.")
        if data.get("jira_linked") != url:
            self.jira.link(key, url)
            self.store.update(key, version, jira_linked=url)

    def check_merge(self, ticket):
        key, version, data = ticket["key"], ticket["version"], ticket["data"]
        self.boundary(key, version)
        self.announce_pr(key, version)
        pr = self.github.pr(data["pr"])
        if pr.get("merged"):
            self.store.merged(key, pr.get("merge_commit_sha"), pr["html_url"])
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
        for ticket in self.store.all():
            if self.stop.is_set():
                return
            key, version = ticket["key"], ticket["version"]
            if ticket["data"].get("retry_at", 0) > time.time():
                continue
            try:
                if ticket["state"] == "ready":
                    self.work(ticket)
                    self.store.remote_success(key, version)
                    return
                if ticket["state"] == "awaiting_approval":
                    # A new specification/head bypasses the old status schedule.
                    token = (version, ticket["data"].get("head"))
                    previous = getattr(self, "status_schedule", {}).get(key)
                    now = time.monotonic()
                    if previous is None or previous[0] != token or now >= previous[1]:
                        if not hasattr(self, "status_schedule"):
                            self.status_schedule = {}
                        # Schedule before I/O so failures cannot create a tight retry loop.
                        self.status_schedule[key] = (token, now + self.cfg.status_poll_seconds)
                        self.check_merge(ticket)
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
                    target = "blocked"
                    self.store.update(key, version, target, error=str(exc)[:2000], notice=(f"error:{version}:{time.time_ns()}",
                              f"Work stopped: {type(exc).__name__}: {str(exc)[:1500]}\nSaved work is retained. Resolve the blocker and comment /forge retry."))
                except Superseded:
                    pass

    def run(self):
        self.store.recover()
        # Initial Jira reconciliation must succeed before any recovered coding is resumed.
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
