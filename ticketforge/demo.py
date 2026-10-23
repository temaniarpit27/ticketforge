"""Offline integration demonstration. Scripted actors, never real model execution."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import Config
from .coordinator import Coordinator
from .runtime import Repository, process
from .store import Store


KEY = "DEMO-1"


class DemoJira:
    def __init__(self):
        self.description = "Implement shipping_cost(subtotal). Charge 10 below the free-shipping threshold and 0 at or above it."
        self.human = []
        self.posts = []
        self.links = []

    def discover(self):
        return [KEY]

    def say(self, text):
        self.human.append({"id": str(len(self.human) + 1), "author": "human", "text": text})

    def snapshot(self, key):
        return {"summary": "Implement shipping cost", "description": self.description,
                "notes": copy.deepcopy([c for c in self.human if not c["text"].startswith("/forge ")])}, copy.deepcopy(self.human)

    def post_once(self, key, identity, body):
        if not any(p["id"] == identity for p in self.posts):
            self.posts.append({"id": identity, "key": key, "body": body})

    def link(self, key, url):
        if url not in self.links:
            self.links.append(url)


class DemoRepository(Repository):
    def context(self, workspace):
        # Internal coordinator fixture only; real Repository.context enforces Go support.
        return {"language": "scripted-test-fixture", "warnings": []}

    def prepare(self, key):
        workspace = self.root / key / "coder"
        branch = "ticketforge/" + key.lower()
        if not (workspace / ".git").exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self.git("clone", "--no-hardlinks", "--no-local", self.cfg.repo, str(workspace))
            self.git("checkout", "-b", branch, cwd=workspace)
            self.git("update-ref", "refs/ticketforge/base", "HEAD", cwd=workspace)
        return workspace, branch

    def publish(self, workspace, branch):
        pass  # Deliberately no external push in the offline demonstration.


class DemoSandbox:
    def run(self, workspace, command, guard=lambda: None):
        guard()
        # Only a fixed trusted test invocation is supported; never executes model commands.
        if command != "python -m unittest discover -s tests -v":
            raise ValueError("Offline demo accepts only its built-in test command")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return process([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                       cwd=workspace, env=env, check=False, cancelled=guard)


class DemoGitHub:
    def __init__(self, repo):
        self.repo, self.record, self.merge_calls = repo, None, []
        self.check_results = {"tests": "success"}
        self.review_results = []

    def ensure_pr(self, branch, title, body):
        workspace, _ = self.repo.prepare(KEY)
        head = self.repo.git("rev-parse", "HEAD", cwd=workspace)
        self.record = {"number": 1, "html_url": "https://github.example/demo/repo/pull/1", "head": {"sha": head},
                       "state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean"}
        return copy.deepcopy(self.record)

    def pr(self, number):
        return copy.deepcopy(self.record)

    def checks(self, sha):
        return self.check_results

    def reviews(self, number):
        return self.review_results

    def merge(self, number, sha):
        if sha != self.record["head"]["sha"]:
            raise ValueError("Head changed")
        self.merge_calls.append(sha)
        self.record.update(merged=True, state="closed", merge_commit_sha=sha)
        return {"merged": True, "sha": sha}


class DemoAgents:
    def __init__(self):
        self.calls = []

    def run(self, role, workspace, task, guard, trace):
        guard()
        self.calls.append({"role": role, "workspace": str(workspace), "task": copy.deepcopy(task)})
        notes = task["specification"]["notes"]
        result = {"summary": "Implemented threshold behavior and verified boundary cases.", "questions": [], "findings": []}
        if not notes:
            return {**result, "status": "needs_input", "questions": ["What subtotal qualifies for free shipping?"]}
        threshold = 150 if any("150" in n["text"] for n in notes) else 100
        if role == "coder":
            # First attempt deliberately has a boundary bug; independent review catches it.
            operator = ">=" if task.get("review_findings_or_failed_checks") or threshold == 150 else ">"
            (workspace / "shipping.py").write_text(f"def shipping_cost(subtotal):\n    return 0 if subtotal {operator} {threshold} else 10\n")
            if operator == ">=":
                (workspace / "tests" / "test_shipping.py").write_text(
                    "import unittest\nfrom shipping import shipping_cost\n\nclass ShippingTests(unittest.TestCase):\n"
                    f"    def test_below(self): self.assertEqual(shipping_cost({threshold - 1}), 10)\n"
                    f"    def test_boundary(self): self.assertEqual(shipping_cost({threshold}), 0)\n"
                    f"    def test_above(self): self.assertEqual(shipping_cost({threshold + 1}), 0)\n")
            return {**result, "status": "ready"}
        code = (workspace / "shipping.py").read_text()
        if " >= " not in code:
            return {**result, "status": "changes_requested", "findings": [
                f"[P1] shipping.py:2 — subtotal exactly {threshold} is charged 10, but the requirement says free at the threshold. Use an inclusive comparison and test the boundary."]}
        return {**result, "status": "pass"}


def fixture(root):
    source = root / "source"
    source.mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "shipping.py").write_text("def shipping_cost(subtotal):\n    raise NotImplementedError\n")
    (source / "tests" / "test_shipping.py").write_text(
        "import unittest\nfrom shipping import shipping_cost\n\nclass ShippingTests(unittest.TestCase):\n"
        "    def test_below(self): self.assertEqual(shipping_cost(50), 10)\n"
        "    def test_above(self): self.assertEqual(shipping_cost(200), 0)\n")
    process(["git", "init", "-b", "main", str(source)])
    cfg = Config(repo=str(source), jira_url="https://jira.example", jira_jql="project = DEMO",
                 allowed_jira_accounts=["human"], github_repo="demo/repo", coder_model="scripted", reviewer_model="scripted",
                 state_dir=str(root / "state"), auto_merge=True, required_checks=["tests"],
                 verification_commands=["python -m unittest discover -s tests -v"])
    repo = DemoRepository(cfg)
    repo.git("add", ".", cwd=source)
    repo.git("-c", "user.name=Demo", "-c", "user.email=demo@localhost", "-c", "commit.gpgsign=false",
             "commit", "-m", "Initial fixture", cwd=source)
    store = Store(root / "state" / "state.sqlite3")
    jira, github, agents, sandbox = DemoJira(), DemoGitHub(repo), DemoAgents(), DemoSandbox()
    coordinator = Coordinator(cfg, store, jira, github, repo, agents, sandbox)
    return coordinator


def run_demo(parent):
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=parent)).resolve()
    c = fixture(root)
    try:
        c.poll()
        c.tick()  # Coder asks a question on Jira.
        assert c.store.get(KEY)["state"] == "waiting_input"
        c.jira.say("Use a threshold of 100.")
        c.poll()
        c.tick()  # Reviewer finds a boundary bug.
        assert c.store.get(KEY)["state"] == "ready"
        c.tick()  # Coder fixes; reviewer passes; PR raised.
        assert c.store.get(KEY)["state"] == "awaiting_approval"
        old_head = c.store.get(KEY)["data"]["head"]
        c.jira.say("Update the threshold to 150 instead.")
        c.poll()
        c.tick()
        ticket = c.store.get(KEY)
        assert ticket["state"] == "awaiting_approval" and ticket["data"]["head"] != old_head
        c.jira.say("/forge approve " + old_head)
        c.poll()
        assert not c.store.get(KEY)["data"].get("approval")
        c.jira.say("/forge approve " + ticket["data"]["head"])
        c.poll()
        c.tick()
        c.flush()
        assert c.store.get(KEY)["state"] == "merged"
        (root / "jira-transcript.json").write_text(json.dumps({"human_comments": c.jira.human,
                                                            "agent_comments": c.jira.posts,
                                                            "links": c.jira.links}, indent=2))
        print("Offline demo passed: clarification → coding → review finding → repair → PR → new input → revised PR → stale approval rejected → approval → merge.")
        print("Actors were scripted; no Jira, GitHub, model API, or Docker calls were made.")
        print(f"Saved repository, durable state, and Jira transcript: {root}")
    finally:
        c.store.close()
