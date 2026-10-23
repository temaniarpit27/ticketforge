import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ticketforge.config import Config, load_env
from ticketforge.integrations import GitHub, Jira, adf_text, to_adf
from ticketforge.runtime import Agents, ExecutionError, Sandbox, process
from ticketforge.store import Superseded


class BoundaryTests(unittest.TestCase):
    def sandbox(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Sandbox(SimpleNamespace(command_timeout=30, sandbox_image="test:local", state_dir=temp.name,
                                       sandbox_file_size_mb=1024, sandbox_tmp_mb=1024))

    def test_adf_roundtrip(self):
        self.assertEqual(adf_text(to_adf("Hello\n\nQuestion?")), "Hello\n\nQuestion?\n")

    def test_jira_excludes_bot_and_unauthorized_authors(self):
        jira = Jira.__new__(Jira)
        jira.cfg = SimpleNamespace(allowed_jira_accounts=["human"], require_agent_assignment=True)
        jira.bot = "bot"
        jira.http = SimpleNamespace(call=lambda *a, **kw: {"fields": {"summary": "Task", "description": to_adf("Details"),
                                                                      "assignee": {"accountId": "bot"}}})
        jira.comments = lambda key: [{"id": str(i), "author": {"accountId": author}, "body": to_adf(body)}
                                     for i, (author, body) in enumerate([
                                         ("bot", "I am working"), ("stranger", "/forge approve " + "a" * 40),
                                         ("human", "Use 150"), ("human", "/forge pause")])]
        snapshot, comments = jira.snapshot("APP-1")
        self.assertTrue(snapshot["assigned_to_agent"])
        self.assertEqual([n["text"] for n in snapshot["notes"]], ["Use 150"])
        self.assertEqual(len(comments), 2)

    def test_jira_bot_marker_reconciles_only_own_comments(self):
        jira = Jira.__new__(Jira)
        jira.bot = "bot"
        calls = []
        jira.http = SimpleNamespace(call=lambda *args: calls.append(args))
        jira.comments = lambda key: [{"author": {"accountId": "human"}, "body": to_adf("[ticketforge:id]")}]
        jira.post_once("APP-1", "id", "body")
        self.assertEqual(len(calls), 1)
        jira.comments = lambda key: [{"author": {"accountId": "bot"}, "body": to_adf("[ticketforge:id]")}]
        jira.post_once("APP-1", "id", "body")
        self.assertEqual(len(calls), 1)

    def test_newest_check_result_wins(self):
        github = GitHub.__new__(GitHub)
        github.root = "/repos/org/repo"
        def pages(path, field=None):
            if "check-runs" in path:
                return [{"id": 2, "name": "tests", "status": "completed", "conclusion": "failure"},
                        {"id": 1, "name": "tests", "status": "completed", "conclusion": "success"}]
            return [{"id": 4, "context": "lint", "state": "pending"}, {"id": 3, "context": "lint", "state": "success"}]
        github.pages = pages
        self.assertEqual(github.checks("a" * 40), {"tests": "failure", "lint": "pending"})

    def test_docker_has_no_network_or_service_secrets(self):
        sandbox = self.sandbox()
        calls = []
        def fake_process(args, **kwargs):
            calls.append((args, kwargs))
            return {"exit_code": 0, "output": "ok"}
        with tempfile.TemporaryDirectory() as temp, patch("ticketforge.runtime.process", fake_process):
            root = Path(temp)
            (root / ".git").mkdir()
            sandbox.run(root, "python -m unittest")
        args, kwargs = calls[0]
        self.assertIn("--network=none", args)
        self.assertIn("--cap-drop=ALL", args)
        self.assertIn("--read-only", args)
        self.assertIn("fsize=1073741824:1073741824", args)
        self.assertEqual(args[-3:-1], ["sh", "-c"])
        self.assertIn("--env=GOCACHE=/cache/go-build", args)
        self.assertTrue(any("/workspace/.git,readonly" in a for a in args))
        self.assertFalse(any("TOKEN" in a or "API_KEY" in a for a in args))
        self.assertEqual(calls[-1][0][:3], ["docker", "rm", "-f"])

    def test_env_file_never_evaluates_shell(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / ".env"
            path.write_text("TOKEN='$(touch /tmp/should-never-run)'\n")
            load_env(path)
            self.assertEqual(os.environ["TOKEN"], "$(touch /tmp/should-never-run)")

    def test_caches_are_shared_across_tickets_but_never_across_roles_or_images(self):
        sandbox = self.sandbox()
        root = Path(sandbox.cfg.state_dir) / "workspaces"
        cases = [
            ("coder", root / "APP-1" / "coder", root / "APP-2" / "coder"),
            ("verify", root / "APP-1" / "coder", root / "APP-2" / "coder"),
            ("reviewer", root / "APP-1" / "review-v1-random", root / "APP-2" / "review-v3-other"),
        ]
        for role, first, second in cases:
            with self.subTest(role):
                self.assertEqual(sandbox.cache_dir(first, role), sandbox.cache_dir(second, role))
        ticket = root / "APP-1" / "coder"
        caches = {sandbox.cache_dir(ticket, role) for role in ("coder", "reviewer", "verify", "assessor")}
        self.assertEqual(len(caches), 4)
        outside = Path(sandbox.cfg.state_dir).parent / "elsewhere"
        self.assertNotEqual(sandbox.cache_dir(outside, "coder"), sandbox.cache_dir(ticket, "coder"))
        before = sandbox.cache_dir(ticket, "coder")
        sandbox.cfg.sandbox_image = "other-toolchain:local"
        self.assertNotEqual(before, sandbox.cache_dir(ticket, "coder"))

    def test_auto_merge_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".git").mkdir()
            cfg = Config(repo=temp, jira_url="https://jira.example", jira_jql="project = APP",
                         allowed_jira_accounts=["human"], github_repo="org/repo", coder_model="a", reviewer_model="b",
                         verification_commands=["test"], auto_merge=True)
            with self.assertRaisesRegex(ValueError, "Automatic merging"):
                cfg.validate()

    def test_agent_tool_result_loop_and_independent_model_selection(self):
        cfg = SimpleNamespace(max_agent_steps=4, max_input_chars=100000, max_output_tokens=1000, reviewer_max_output_tokens=2000,
                              coder_model="coder", reviewer_model="reviewer", coder_provider="anthropic", reviewer_provider="anthropic")
        agents = Agents.__new__(Agents)
        agents.cfg = cfg
        agents.skill_root = Path(__file__).resolve().parents[1] / "ticketforge" / "skills"
        agents.sandbox = SimpleNamespace(run=lambda *a, **kw: {"exit_code": 0, "output": "evidence"})
        requests = []
        def call(method, path, body, **kwargs):
            requests.append(copy.deepcopy(body))
            if len(requests) == 1:
                return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "one", "name": "shell", "input": {"command": "ls"}}]}
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "two", "name": "finish", "input": {
                "status": "pass", "summary": "No actionable findings", "questions": [], "findings": [], "resolutions": []}}]}
        agents.clients = {"anthropic": SimpleNamespace(call=call)}
        result = agents.run("reviewer", Path("/unused"), {"specification": "task"}, lambda: None, lambda e: None)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(requests[0]["model"], "reviewer")
        self.assertEqual(requests[0]["max_tokens"], 2000)
        self.assertEqual(requests[1]["messages"][-1]["content"][0]["tool_use_id"], "one")
        self.assertIn("evidence", requests[1]["messages"][-1]["content"][0]["content"])

    def test_model_truncation_cannot_be_accepted_as_success(self):
        cfg = SimpleNamespace(max_agent_steps=1, max_input_chars=100000, max_output_tokens=10, reviewer_max_output_tokens=10,
                              coder_model="coder", reviewer_model="reviewer", coder_provider="anthropic", reviewer_provider="anthropic")
        agents = Agents.__new__(Agents)
        agents.cfg = cfg
        agents.skill_root = Path(__file__).resolve().parents[1] / "ticketforge" / "skills"
        agents.clients = {"anthropic": SimpleNamespace(call=lambda *a, **kw: {"stop_reason": "max_tokens", "content": []})}
        with self.assertRaises(ExecutionError):
            agents.run("coder", Path("/unused"), {}, lambda: None, lambda e: None)

    def test_container_cleanup_is_attempted_on_cancellation(self):
        sandbox = self.sandbox()
        calls = []
        def fake_process(args, **kwargs):
            calls.append(args)
            if "run" in args:
                raise Superseded("new input")
            return {"exit_code": 0, "output": ""}
        with tempfile.TemporaryDirectory() as temp, patch("ticketforge.runtime.process", fake_process):
            root = Path(temp)
            (root / ".git").mkdir()
            with self.assertRaises(Superseded):
                sandbox.run(root, "sleep 10")
        self.assertEqual(calls[-1][:3], ["docker", "rm", "-f"])

    def test_large_host_command_output_is_not_silently_truncated(self):
        import sys
        with self.assertRaisesRegex(ExecutionError, "result limit"):
            process([sys.executable, "-c", "print('x' * 25000)"])

    def test_git_auth_helper_works_from_a_project_path_with_spaces(self):
        import shutil
        import subprocess
        from ticketforge.runtime import Repository
        with tempfile.TemporaryDirectory(prefix="ticketforge path ") as temp:
            cfg = SimpleNamespace(state_dir=temp)
            repo = Repository(cfg)
            helper = Path(temp) / "helper with spaces.py"
            shutil.copy2(repo.askpass, helper)
            helper.chmod(0o700)
            repo.askpass = helper
            captured = {}
            def capture(args, **kwargs):
                captured.update(kwargs["env"])
                return {"output": "", "exit_code": 0}
            with patch.dict(os.environ, {"GITHUB_TOKEN": "offline-dummy-token"}), patch("ticketforge.runtime.process", capture):
                repo.git("version")
            value = subprocess.run(["git", "-c", "credential.helper=", "credential", "fill"],
                                   input="protocol=https\nhost=github.com\n\n", text=True,
                                   capture_output=True, env=captured, timeout=10)
            self.assertEqual(value.returncode, 0, value.stderr)
            self.assertIn("username=x-access-token", value.stdout)
            self.assertIn("password=offline-dummy-token", value.stdout)


if __name__ == "__main__":
    unittest.main()
