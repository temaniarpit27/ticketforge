from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from .store import Store, Superseded

log = logging.getLogger(__name__)


class Coordinator:
    def __init__(self, cfg, store: Store, jira, github, repo, agents, sandbox):
        self.cfg, self.store, self.jira, self.github = cfg, store, jira, github
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
        round_number = ticket["data"].get("round", 0) + 1
        if round_number > self.cfg.max_review_rounds:
            self.store.update(key, version, "blocked", notice=(f"round-limit:{version}", "Coding/review limit reached. Please inspect the findings. Add guidance here or use /forge retry after resolving the blocker."))
            return
        self.store.update(key, version, "coding", round=round_number, approval=None, notice=(f"started:{version}:{round_number}",
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
        coded = self.agents.run("coder", workspace, task, guard, tracer)
        self.boundary(key, version)
        if self.result(key, version, "coder", coded):
            return
        verification = []
        for command in self.cfg.verification_commands:
            guard()
            value = self.sandbox.run(workspace, command, guard)
            verification.append({"command": command, **value})
            tracer({"role": "coordinator", "verification": verification[-1]})
        self.boundary(key, version)
        failed = [v for v in verification if v["exit_code"] != 0]
        if failed:
            self.store.update(key, version, "ready", feedback=failed, verification=verification,
                              notice=(f"checks-failed:{version}:{round_number}", "Verification failed. Returning evidence to the coder for repair:\n" +
                      "\n".join(f"{v['command']}: exit {v['exit_code']}\n{v['output'][:2500]}" for v in failed)))
            return
        head, base = self.repo.commit(workspace, key, version)
        self.boundary(key, version)
        self.store.update(key, version, "reviewing", head=head, base=base, branch=branch,
                          verification=verification, coder_result=coded, notice=(f"review-start:{version}:{head}:{round_number}",
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
        body = (f"Implements {key}, specification v{version}.\n\nJira: {self.cfg.jira_url.rstrip('/')}/browse/{key}\n\n"
                f"Commit: {head}\n\nSummary:\n{coded['summary']}\n\n"
                "Verification run by coordinator:\n" + "\n".join(f"- `{v['command']}`: exit {v['exit_code']}" for v in verification) +
                f"\n\nIndependent review:\n{reviewed['summary']}\n\nHuman review is required. Discuss changes and approve on Jira.")
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
                  f"Please review the diff. Request changes here, or approve this exact commit with:\n/forge approve {head}\n\n"
                  "New input invalidates earlier approval. GitHub checks and repository rules must also pass.")
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
            self.post(key, f"unexpected-head:{version}:{head}", "The PR branch changed outside TicketForge. Automatic merge is blocked. Reconcile the branch before retrying.")
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
        current = self.store.get(key)["data"]
        approval = current.get("approval")
        if not approval or not self.cfg.auto_merge:
            if approval and not self.cfg.auto_merge:
                self.post(key, f"merge-disabled:{version}:{head}", "Approval is recorded. Automatic merging is disabled in configuration; merge manually or enable it after configuring required checks and repository rules.")
            return
        if approval.get("sha") != head or approval.get("version") != version:
            return
        if current.get("reviewed_head") != head or current.get("reviewed_version") != version:
            return
        if any(checks.get(name) != "success" for name in self.cfg.required_checks):
            return
        # Do not override a human change request, including one on an older commit.
        latest_reviews = {}
        for r in sorted(reviews, key=lambda r: r["id"]):
            if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest_reviews[r["user"]["login"]] = r["state"]
        if "CHANGES_REQUESTED" in latest_reviews.values() or pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
            self.post(key, f"merge-blocked:{version}:{head}:{digest}", "Approval recorded, but GitHub reports a review, conflict, or branch-policy blocker. Resolve it before merging.")
            return
        self.boundary(key, version)
        self.store.update(key, version, "merging", notice=(f"merge-start:{version}:{head}", f"Approval and checks confirmed for {head}. Starting merge; this is the final input cut-off for this run."))
        self.flush()
        self.boundary(key, version)
        result = self.github.merge(pr["number"], head)
        # Reconcile on later polling if input arrived during the irreversible remote call.
        self.store.merged(key, result.get("sha"), pr["html_url"])

    def tick(self):
        for ticket in self.store.all():
            if self.stop.is_set():
                return
            key, version = ticket["key"], ticket["version"]
            try:
                if ticket["state"] == "ready":
                    self.work(ticket)
                    return
                if ticket["state"] == "awaiting_approval":
                    self.check_merge(ticket)
                elif ticket["state"] == "merging":
                    remote = self.github.pr(ticket["data"]["pr"])
                    if remote.get("merged"):
                        self.store.merged(key, remote.get("merge_commit_sha"), remote["html_url"])
                    else:
                        target = "awaiting_approval" if ticket["data"].get("reviewed_version") == version else "ready"
                        self.store.update(key, version, target)
            except Superseded:
                log.info("Task %s superseded or stopped", key)
            except Exception as exc:
                log.exception("Task %s failed", key)
                try:
                    # An uncertain merge result must be reconciled, not blindly restarted.
                    target = "merging" if self.store.get(key)["state"] == "merging" else "blocked"
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
            while not self.stop.wait(1):
                try:
                    if time.monotonic() >= next_poll:
                        self.poll()
                        next_poll = time.monotonic() + self.cfg.poll_seconds
                    self.flush()
                except Exception:
                    log.exception("Background integration failed; durable messages remain queued")
        thread = threading.Thread(target=background, name="jira-events", daemon=True)
        thread.start()
        try:
            while not self.stop.is_set():
                self.tick()
                self.stop.wait(1)
        finally:
            self.stop.set()
            thread.join(timeout=95)
