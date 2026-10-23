import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ticketforge.project import UnsupportedRepository, go_dockerfile, init_profile, inspect_project, safe_text


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project with spaces"
        self.root.mkdir()
        (self.root / "go.mod").write_text("module example.com/service\n\ngo 1.23\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_guides_still_discovers_go_and_reports_unknown_product_context(self):
        (self.root / "internal").mkdir()
        (self.root / "internal" / "payments").mkdir()
        context = inspect_project(self.root)
        self.assertEqual(context["module"], "example.com/service")
        self.assertEqual(context["language"], "go")
        self.assertIn("internal/payments/", context["structure"])
        self.assertTrue(context["warnings"])
        self.assertIn("Jira", context["policy"])

    def test_reads_actual_project_guidance_with_provenance(self):
        (self.root / "TICKETFORGE.md").write_text("All amounts are integer minor units.")
        context = inspect_project(self.root)
        self.assertEqual(context["documents"][0]["path"], "TICKETFORGE.md")
        self.assertIn("minor units", context["documents"][0]["text"])
        self.assertEqual(context["warnings"], [])

    def test_unsupported_language_is_explicitly_blocked(self):
        (self.root / "go.mod").unlink()
        (self.root / "package.json").write_text("{}")
        with self.assertRaisesRegex(UnsupportedRepository, "Node/JavaScript"):
            inspect_project(self.root)

    def test_unknown_project_does_not_invent_a_language(self):
        (self.root / "go.mod").unlink()
        with self.assertRaisesRegex(UnsupportedRepository, "could not be established"):
            inspect_project(self.root)

    def test_go_workspace_and_local_module_replacements_are_not_silently_supported(self):
        (self.root / "go.work").write_text("go 1.23\nuse ./service\n")
        with self.assertRaisesRegex(UnsupportedRepository, "workspaces"):
            inspect_project(self.root)
        (self.root / "go.work").unlink()
        (self.root / "go.mod").write_text("module example.com/service\ngo 1.23\nreplace example.com/local => ../local\n")
        with self.assertRaisesRegex(UnsupportedRepository, "Local Go module"):
            inspect_project(self.root)

    def test_context_reader_does_not_follow_symlinks_outside_repository(self):
        private = Path(self.tmp.name) / "private.txt"
        private.write_text("not project context")
        (self.root / "README.md").symlink_to(private)
        self.assertIsNone(safe_text(self.root, "README.md"))

    def test_large_context_document_is_bounded_and_marked(self):
        (self.root / "README.md").write_text("x" * 10000)
        document = inspect_project(self.root)["documents"][0]
        self.assertEqual(len(document["text"]), 6000)
        self.assertTrue(document["truncated"])

    def test_profile_is_external_and_infers_github_origin(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", "git@github.com:example/service.git"], check=True)
        before = set(self.root.iterdir())
        directory = Path(self.tmp.name) / "profile"
        cfg, context = init_profile(self.root, directory)
        self.assertEqual(cfg.github_repo, "example/service")
        self.assertEqual(set(self.root.iterdir()), before)
        stored = json.loads((directory / "config.json").read_text())
        self.assertEqual(stored["repo"], str(self.root.resolve()))
        self.assertIn("go test -race ./...", stored["verification_commands"])
        docker = (directory / "Dockerfile.sandbox").read_text()
        self.assertIn("ARG GO_VERSION=1.23", docker)
        self.assertIn("COPY go.mod ./", docker)
        self.assertNotIn("COPY .", docker)
        self.assertIn("GOPROXY=off", docker)
        self.assertTrue((directory / "PROJECT.md").exists())
        self.assertEqual(stored["project_guide"], "PROJECT.md")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            init_profile(self.root, directory)

    def test_explicit_toolchain_and_checksum_file_are_used(self):
        (self.root / "go.mod").write_text("module example.com/service\ngo 1.23\ntoolchain go1.24.2\n")
        context = inspect_project(self.root)
        docker = go_dockerfile(context, True)
        self.assertIn("ARG GO_VERSION=1.24.2", docker)
        self.assertIn("COPY go.mod go.sum ./", docker)

    def test_claude_and_agent_guides_and_skills_are_discovered_without_execution(self):
        for name in ("CLAUDE.md", "AGENT.md", "agent.md"):
            (self.root / name).write_text("Project-specific factual guidance")
        skill = self.root / ".claude" / "skills" / "go-testing"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: go-testing\ndescription: Test Go handlers\n---\nRead the tests before changes.\n")
        context = inspect_project(self.root)
        self.assertTrue({"CLAUDE.md", "AGENT.md"}.issubset({d["path"] for d in context["documents"]}))
        self.assertEqual(context["skills"][0]["description"], "Test Go handlers")
        self.assertNotIn("instructions", context["skills"][0])  # Loaded on demand via sandbox.


if __name__ == "__main__":
    unittest.main()
