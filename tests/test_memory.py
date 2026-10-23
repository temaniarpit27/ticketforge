"""What the agents remember: coder notes, Git history, the repo map, and approved lessons.

Also checks the reviewer stays independent: it never receives anything the coder wrote.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ticketforge import review
from ticketforge.config import Config
from ticketforge.coordinator import save_lessons
from ticketforge.demo import KEY, fixture
from ticketforge.runtime import LEARNED_HEADING, Repository

LESSONS = ["Run make generate after editing .proto files", "Test fixtures live in testdata/"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = fixture(Path(self.tmp.name))
        self.guide = Path(self.tmp.name) / "PROJECT.md"
        self.guide.write_text("# Guide\n\nShipping rules live in shipping.py.\n")
        self.c.cfg.project_guide = str(self.guide)

    def tearDown(self):
        self.c.store.close()
        self.tmp.cleanup()

    def say(self, text):
        self.c.jira.say(text)
        self.c.poll()

    def calls(self, role):
        return [c["task"] for c in self.c.agents.calls if c["role"] == role]

    def posts(self):
        self.c.flush()
        return "\n".join(p["body"] for p in self.c.jira.posts)

    def propose_lessons(self):
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            value = original(role, workspace, task, guard, trace, on_finish)
            return {**value, "lessons": LESSONS} if role == "coder" else value
        self.c.agents.run = run

    def merge(self):
        self.c.github.human_merge(1, self.c.store.get(KEY)["data"]["head"])
        self.c.tick()


class NotesAndHistoryTests(Base):
    def test_next_round_gets_coder_notes_and_history_but_reviewer_gets_neither(self):
        asked, context = [], self.c.repo.context
        self.c.repo.context = lambda workspace, reviewer=False: (asked.append((Path(workspace).name, reviewer)), context(workspace))[1]
        self.say("Use a threshold of 100.")
        self.c.tick()
        self.c.tick()
        first, second = self.calls("coder")
        self.assertNotIn("your_notes_from_last_round", first)
        self.assertEqual(first["history"]["commits_on_this_ticket"], [])
        self.assertIn("shipping.py", second["your_notes_from_last_round"]["text"])
        self.assertEqual(len(second["history"]["commits_on_this_ticket"]), 1)
        self.assertTrue(any("shipping.py" in line for line in second["history"]["files_changed_so_far"]))
        self.assertTrue(second["history"]["recent_commits_before_this_ticket"])
        for task in self.calls("reviewer"):
            for private in ("your_notes_from_last_round", "history", "coder_result", "assessment"):
                self.assertNotIn(private, task)
        # Review clones always ask for the reviewer's version of the context (no lessons).
        self.assertTrue(all(reviewer == name.startswith("review-") for name, reviewer in asked))
        self.assertIn(True, [reviewer for _, reviewer in asked])


class LessonTests(Base):
    def test_approved_lessons_are_saved_to_the_guide_only_after_merge(self):
        self.propose_lessons()
        self.say("Use a threshold of 150.")
        self.c.tick()
        self.assertIn("/forge learn", self.posts())
        self.say("/forge learn 2")
        self.assertEqual(self.c.store.get(KEY)["data"]["lessons_approved"], [LESSONS[1]])
        self.assertNotIn(LESSONS[1], self.guide.read_text())  # not before merge
        self.merge()
        text = self.guide.read_text()
        self.assertTrue(text.startswith("# Guide\n\nShipping rules live in shipping.py.\n"))
        self.assertIn(f"{LEARNED_HEADING}\n\n- {LESSONS[1]} ({KEY})\n", text)
        self.assertNotIn(LESSONS[0], text)
        self.assertIn("Saved 1 note", self.posts())

    def test_nothing_is_saved_without_approval(self):
        self.propose_lessons()
        self.say("Use a threshold of 150.")
        self.c.tick()
        before = self.guide.read_text()
        self.merge()
        self.assertEqual(self.guide.read_text(), before)

    def test_invalid_or_early_learn_commands_are_explained(self):
        self.propose_lessons()
        self.say("Use a threshold of 150.")
        self.say("/forge learn")  # no PR yet
        self.assertIn("no proposed notes", self.posts())
        self.c.tick()
        for command in ("/forge learn 3", "/forge learn one", "/forge learn 0"):
            self.say(command)
            self.assertIsNone(self.c.store.get(KEY)["data"].get("lessons_approved"))
        self.assertIn("numbers between 1 and 2", self.posts())

    def test_new_requirements_cancel_an_earlier_approval(self):
        self.propose_lessons()
        self.say("Use a threshold of 150.")
        self.c.tick()
        self.say("/forge learn")
        self.say("Also log the computed cost.")
        self.assertIsNone(self.c.store.get(KEY)["data"]["lessons_approved"])

    def test_without_a_project_guide_you_are_told_instead(self):
        self.c.cfg.project_guide = ""
        self.propose_lessons()
        self.say("Use a threshold of 150.")
        self.c.tick()
        self.say("/forge learn")
        self.merge()
        self.assertIn("set project_guide", self.posts())

    def test_failed_lesson_write_is_retried_with_back_off(self):
        # Reviews: a failed write was first never retried, then retried every second.
        import time as clock
        from unittest.mock import patch
        import ticketforge.coordinator as coordinator
        self.propose_lessons()
        self.say("Use a threshold of 150.")
        self.c.tick()
        self.say("/forge learn 1")
        real = coordinator.save_lessons
        with patch.object(coordinator, "save_lessons", side_effect=OSError("disk full")) as failing:
            self.merge()
            for _ in range(5):
                self.c.tick()
        self.assertEqual(failing.call_count, 1)  # the merge tick tried once; the next ticks waited
        data = self.c.store.get(KEY)["data"]
        self.assertEqual(data["lessons_failures"], 1)
        self.assertEqual(data["lessons_approved"], [LESSONS[0]])  # kept for later
        self.assertEqual(self.posts().count("Could not save the approved notes"), 1)
        later = clock.time() + 61
        with patch("ticketforge.coordinator.time.time", return_value=later), \
             patch.object(coordinator, "save_lessons", side_effect=OSError("disk full")) as failing:
            self.c.tick()
            self.c.tick()
        self.assertEqual(failing.call_count, 1)
        self.assertEqual(self.c.store.get(KEY)["data"]["lessons_failures"], 2)
        self.assertGreaterEqual(self.c.store.get(KEY)["data"]["lessons_retry_at"], later + 120)  # doubled
        self.assertEqual(self.posts().count("Could not save the approved notes"), 1)  # told once
        with patch("ticketforge.coordinator.time.time", return_value=later + 121), \
             patch.object(coordinator, "save_lessons", side_effect=real) as saver:
            self.c.tick()
            self.c.tick()
        self.assertEqual(saver.call_count, 1)
        self.assertEqual(self.guide.read_text().count(LESSONS[0]), 1)
        self.assertTrue(self.c.store.get(KEY)["data"]["lessons_saved"])

    def test_crash_between_write_and_record_does_not_duplicate(self):
        save_lessons(self.guide, KEY, [LESSONS[0]])
        save_lessons(self.guide, KEY, [LESSONS[0], LESSONS[1]])  # the retry after a crash
        text = self.guide.read_text()
        self.assertEqual(text.count(LESSONS[0]), 1)
        self.assertEqual(text.count(LESSONS[1]), 1)

    def test_save_lessons_keeps_one_heading_and_one_line_each(self):
        save_lessons(self.guide, "APP-1", ["first\nline"])
        save_lessons(self.guide, "APP-2", ["second"])
        text = self.guide.read_text()
        self.assertEqual(text.count(LEARNED_HEADING), 1)
        self.assertTrue(text.endswith("- first line (APP-1)\n- second (APP-2)\n"))


class ReviewerIndependenceTests(unittest.TestCase):
    def test_reviewer_context_never_includes_coder_written_lessons(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            (root / "go.mod").write_text("module example.com/m\n\ngo 1.22\n")
            guide = root / "PROJECT.md"
            guide.write_text("# Guide\n\nHuman facts.\n\n" + LEARNED_HEADING + "\n\n- coder lesson (APP-1)\n")
            cfg = Config(repo=temp, jira_url="https://jira.example", jira_jql="project = APP", allowed_jira_accounts=["h"],
                         github_repo="o/r", coder_model="a", reviewer_model="b", verification_commands=["t"],
                         state_dir=str(root / "state"), project_guide=str(guide))
            repo = Repository(cfg)
            coder = {d["path"]: d["text"] for d in repo.context(root)["documents"]}
            reviewer = {d["path"]: d["text"] for d in repo.context(root, reviewer=True)["documents"]}
            self.assertIn("coder lesson", coder["operator-profile:learned-from-tickets"])
            self.assertNotIn("operator-profile:learned-from-tickets", reviewer)
            self.assertIn("Human facts.", reviewer["operator-profile:PROJECT.md"])
            self.assertNotIn("coder lesson", json.dumps(reviewer))


class RepoMapTests(Base):
    SHA = "a" * 40

    def fake_sandbox(self, exit_code=0, output=None):
        cache = Path(self.tmp.name) / "cache"
        cache.mkdir(exist_ok=True)
        runs = []

        def run(workspace, command, guard=lambda: None, *, role="coder", read_only=False):
            runs.append((command, role, read_only))
            if output is not None:
                (cache / "repo-map.json").write_text(output)
            return {"exit_code": exit_code, "output": ""}
        self.c.sandbox = SimpleNamespace(run=run, cache_dir=lambda workspace, role: cache)
        return runs

    def test_map_is_made_once_per_commit_and_reused(self):
        listing = json.dumps({"ImportPath": "example.com/m/store", "Doc": "Package store saves orders.", "GoFiles": ["store.go"]})
        runs = self.fake_sandbox(output=listing)
        context = {"language": "go", "module": "example.com/m"}
        first = self.c.repo_map(Path(self.tmp.name), self.SHA, context)
        second = self.c.repo_map(Path(self.tmp.name), self.SHA, context)
        self.assertEqual(first, second)
        self.assertEqual(first["commit"], self.SHA)
        self.assertEqual(first["packages"][0]["package"], "store")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][1:], ("verify", True))  # read-only, never the coder's cache

    def test_failure_is_remembered_and_never_blocks(self):
        runs = self.fake_sandbox(exit_code=1)
        context = {"language": "go", "module": "example.com/m"}
        self.assertIsNone(self.c.repo_map(Path(self.tmp.name), self.SHA, context))
        self.assertIsNone(self.c.repo_map(Path(self.tmp.name), self.SHA, context))
        self.assertEqual(len(runs), 1)

    def test_skipped_unless_language_confirmed_and_commit_valid(self):
        runs = self.fake_sandbox(output="{}")
        self.assertIsNone(self.c.repo_map(Path(self.tmp.name), self.SHA, {"language": "python"}))
        self.assertIsNone(self.c.repo_map(Path(self.tmp.name), "not-a-sha", {"language": "go"}))
        self.assertEqual(runs, [])

    def test_large_maps_drop_file_names_then_packages(self):
        from ticketforge.project import parse_repo_map
        listing = "\n".join(json.dumps({"ImportPath": f"example.com/m/p{i}", "GoFiles": [f"file{j}.go" for j in range(30)]})
                            for i in range(200))
        # Dropping file names is enough: every package is kept, with file counts instead.
        compact = parse_repo_map(listing, "example.com/m", limit=20000)
        self.assertLessEqual(len(json.dumps(compact)), 20000)
        self.assertEqual((len(compact["packages"]), compact["packages"][0]["files"], compact["truncated"]), (200, 30, False))
        # Still too big: packages are dropped too, and the map says it is incomplete.
        tiny = parse_repo_map(listing, "example.com/m", limit=3000)
        self.assertLessEqual(len(json.dumps(tiny)), 3000)
        self.assertLess(len(tiny["packages"]), 200)
        self.assertTrue(tiny["truncated"])


class CoderResultTests(unittest.TestCase):
    def test_notes_and_lessons_limits(self):
        base = {"status": "ready", "summary": "s", "questions": [], "responses": [], "notes": "", "lessons": []}
        review.validate_finish("coder", base)
        cases = [
            ("notes_too_long", {"notes": "x" * (review.MAX_NOTES + 1)}),
            ("too_many_lessons", {"lessons": ["fact"] * (review.MAX_LESSONS + 1)}),
            ("empty_lesson", {"lessons": ["  "]}),
            ("long_lesson", {"lessons": ["x" * (review.MAX_LESSON_CHARS + 1)]}),
        ]
        for name, change in cases:
            with self.subTest(name), self.assertRaises(ValueError):
                review.validate_finish("coder", {**base, **change})


if __name__ == "__main__":
    unittest.main()
