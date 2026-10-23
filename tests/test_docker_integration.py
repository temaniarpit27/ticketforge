"""Opt-in real engine test. Requested runs fail, never skip, if Docker is unavailable."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ticketforge.config import Config
from ticketforge.project import go_dockerfile
from ticketforge.runtime import Sandbox, process


@unittest.skipUnless(os.environ.get("TICKETFORGE_DOCKER_TESTS") == "1", "real Docker test is opt-in; set TICKETFORGE_DOCKER_TESTS=1")
class DockerGoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        process(["docker", "info"], timeout=15)
        parent = Path(__file__).resolve().parents[1] / ".ticketforge" / "integration"
        parent.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="go-", dir=parent)
        cls.root = Path(cls.temp.name)
        cls.workspace = cls.root / "coder"
        cls.workspace.mkdir()
        cls.image = "ticketforge-go-integration:local"
        (cls.workspace / "go.mod").write_text("module example.com/smoke\n\ngo 1.23\n")
        (cls.workspace / "counter.go").write_text('package smoke\nimport "sync"\ntype Counter struct { mu sync.Mutex; n int }\nfunc (c *Counter) Add() { c.mu.Lock(); defer c.mu.Unlock(); c.n++ }\nfunc (c *Counter) Value() int { c.mu.Lock(); defer c.mu.Unlock(); return c.n }\n')
        (cls.workspace / "counter_test.go").write_text('package smoke\nimport ("sync"; "testing")\nfunc TestConcurrentCounter(t *testing.T) { var c Counter; var w sync.WaitGroup; for i:=0;i<20;i++ { w.Add(1); go func(){ defer w.Done(); c.Add() }() }; w.Wait(); if c.Value()!=20 { t.Fatal(c.Value()) } }\n')
        process(["git", "init", str(cls.workspace)])
        version = os.environ.get("TICKETFORGE_DOCKER_GO_VERSION", "1.27.3")
        (cls.workspace / "Dockerfile").write_text(go_dockerfile({"toolchain_version": version}, False))
        try:
            process(["docker", "build", "-t", cls.image, str(cls.workspace)], timeout=600)
        except Exception:
            cls.temp.cleanup()
            raise
        cls.cfg = Config(repo=str(cls.workspace), jira_url="https://jira.example", jira_jql="project = DEMO",
                         allowed_jira_accounts=["human"], github_repo="example/smoke", coder_model="unused", reviewer_model="unused",
                         state_dir=str(cls.root / "state"), sandbox_image=cls.image)
        cls.sandbox = Sandbox(cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_ok(self, command, workspace=None, **options):
        value = self.sandbox.run(workspace or self.workspace, command, **options)
        self.assertEqual(value["exit_code"], 0, value["output"])
        return value["output"].strip()

    def test_real_go_toolchain_checks_limits_and_isolated_persistent_caches(self):
        self.assertEqual(self.run_ok("id -u"), str(os.getuid()))
        self.run_ok("command -v go && go version")
        self.assertIn("go version", self.sandbox.check()["output"])
        for command in ("go test ./...", "go test -race ./...", "go vet ./..."):
            self.run_ok(command, role="verify")
        self.run_ok('go test -c -o /cache/smoke.test && test "$(wc -c < /cache/smoke.test)" -gt 65536')
        self.run_ok("printf 'coder-only' > /cache/persistence-marker")
        self.assertEqual(self.run_ok("cat /cache/persistence-marker"), "coder-only")
        self.run_ok("test ! -e /cache/persistence-marker", role="verify")
        value = self.sandbox.run(self.workspace, "touch /workspace/forbidden", role="assessor", read_only=True)
        self.assertNotEqual(value["exit_code"], 0, value["output"])
        self.assertFalse((self.workspace / "forbidden").exists())
        reviewer = self.root / "reviewer"
        process(["git", "clone", "--no-local", str(self.workspace), str(reviewer)])
        # Source is intentionally uncommitted; only metadata is needed for this isolation check.
        self.run_ok("test ! -e /cache/persistence-marker", reviewer)
        self.assertNotEqual(self.sandbox.cache_dir(self.workspace), self.sandbox.cache_dir(reviewer))
        self.run_ok("test ! -w /workspace/.git/config && test ! -w /etc/passwd")


if __name__ == "__main__":
    unittest.main()
