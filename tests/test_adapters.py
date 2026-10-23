import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ticketforge.adapters import CodeHost, IssueTracker, LanguageAdapter, GoAdapter, language_adapter, create_integrations, LANGUAGES
from ticketforge.demo import DemoJira, DemoGitHub, fixture
from ticketforge.runtime import Repository


class AdapterTests(unittest.TestCase):
    def test_go_adapter_contract_and_explicit_unsupported_language(self):
        self.assertIsInstance(GoAdapter(), LanguageAdapter)
        with self.assertRaisesRegex(ValueError, "not installed"): language_adapter("python")

    def test_unsupported_tracker_is_rejected_before_external_api_calls(self):
        cfg = SimpleNamespace(issue_tracker="gitlab", code_host="github", language="go")
        with self.assertRaisesRegex(ValueError, "not installed"): create_integrations(cfg)

    def test_repository_can_use_registered_language_without_coordinator_changes(self):
        class ToyLanguage:
            def inspect(self, root): return {"language": "toy", "documents": [], "path": str(root)}
        with tempfile.TemporaryDirectory() as temp, patch.dict(LANGUAGES, {"toy": ToyLanguage}):
            repo = Repository(SimpleNamespace(state_dir=temp, language="toy", project_guide=""))
            self.assertEqual(repo.context(Path(temp))["language"], "toy")

    def test_profile_guide_is_loaded_alongside_repository_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "go.mod").write_text("module example/test\ngo 1.23\n")
            guide = root / "external.md"
            guide.write_text("Amounts are integer minor units.")
            repo = Repository(SimpleNamespace(state_dir=temp, language="go", project_guide=str(guide)))
            self.assertIn("minor units", repo.context(root)["documents"][-1]["text"])
