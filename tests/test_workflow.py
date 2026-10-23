import copy
import tempfile
import threading
import unittest
from pathlib import Path

from ticketforge.demo import KEY, fixture
from ticketforge.store import Store, Superseded


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = fixture(Path(self.tmp.name))
        self.c.poll()

    def tearDown(self):
        self.c.store.close()
        self.tmp.cleanup()

    def ready_pr(self):
        self.c.jira.say("Use a threshold of 100.")
        self.c.poll()
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "ready")  # reviewer finding
        self.c.tick()
        ticket = self.c.store.get(KEY)
        self.assertEqual(ticket["state"], "awaiting_approval")
        return ticket

    def approve(self):
        head = self.c.store.get(KEY)["data"]["head"]
        self.c.jira.say("/forge approve " + head)
        self.c.poll()
        return head

    def test_clarification_review_repair_pr_approval_merge(self):
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "waiting_input")
        ticket = self.ready_pr()
        calls = self.c.agents.calls
        coder = next(x for x in calls if x["role"] == "coder")
        reviewer = next(x for x in calls if x["role"] == "reviewer")
        self.assertNotEqual(coder["workspace"], reviewer["workspace"])
        self.assertNotIn("coder_result", reviewer["task"])
        self.assertNotIn("review_findings_or_failed_checks", reviewer["task"])
        head = self.approve()
        self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.store.get(KEY)["state"], "merged")
        self.assertEqual(self.c.github.merge_calls, [head])
        text = "\n".join(p["body"] for p in self.c.jira.posts)
        for expected in ("Started work", "needs clarification", "requested changes", "PR raised", "Merged:"):
            self.assertIn(expected, text)
        self.assertEqual(len(self.c.jira.links), 1)

    def test_new_input_invalidates_review_and_stale_approval(self):
        old = self.ready_pr()
        self.approve()
        self.c.jira.say("Threshold should be 150.")
        self.c.poll()
        ticket = self.c.store.get(KEY)
        self.assertGreater(ticket["version"], old["version"])
        self.assertIsNone(ticket["data"]["approval"])
        self.c.tick()
        self.c.jira.say("/forge approve " + old["data"]["head"])
        self.c.poll()
        self.assertIsNone(self.c.store.get(KEY)["data"]["approval"])
        self.assertEqual(self.c.github.merge_calls, [])

    def test_input_during_model_call_prevents_stale_tools_and_publication(self):
        original = self.c.agents.run
        def interrupted(role, workspace, task, guard, trace):
            self.c.jira.say("Use 150 and clarify the currency.")
            self.c.sync_one(KEY)
            guard()
            return original(role, workspace, task, guard, trace)
        self.c.agents.run = interrupted
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "ready")
        self.assertIsNone(self.c.github.record)

    def test_pause_during_review_stops_publication(self):
        self.c.jira.say("Use a threshold of 150.")
        self.c.poll()
        original = self.c.agents.run
        def interrupted(role, workspace, task, guard, trace):
            if role == "reviewer":
                self.c.jira.say("/forge pause")
                self.c.sync_one(KEY)
                guard()
            return original(role, workspace, task, guard, trace)
        self.c.agents.run = interrupted
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "paused")
        self.assertIsNone(self.c.github.record)
        self.c.agents.run = original
        self.c.jira.say("/forge resume")
        self.c.poll()
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "awaiting_approval")

    def test_failing_checks_prevent_review_and_pr(self):
        self.c.jira.say("Use a threshold of 150.")
        self.c.poll()
        self.c.sandbox.run = lambda *a: {"exit_code": 1, "output": "meaningful failing check"}
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "ready")
        self.assertIsNone(self.c.github.record)
        self.assertEqual([x["role"] for x in self.c.agents.calls], ["coder"])
        self.assertIn("meaningful failing check", self.c.store.get(KEY)["data"]["feedback"][0]["output"])

    def test_review_loop_is_bounded(self):
        self.c.jira.say("Use a threshold of 150.")
        self.c.poll()
        original = self.c.agents.run
        def always_find(role, *args):
            value = original(role, *args)
            if role == "reviewer":
                return {**value, "status": "changes_requested", "findings": ["Reproducible defect"]}
            return value
        self.c.agents.run = always_find
        for _ in range(self.c.cfg.max_review_rounds + 1):
            self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "blocked")
        self.assertIsNone(self.c.github.record)

    def test_pending_ci_and_human_change_request_block_merge(self):
        self.ready_pr()
        self.approve()
        self.c.github.check_results = {"tests": "in_progress"}
        self.c.tick()
        self.assertEqual(self.c.github.merge_calls, [])
        self.c.github.check_results = {"tests": "success"}
        self.c.github.review_results = [{"id": 1, "user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}]
        self.c.tick()
        self.assertEqual(self.c.github.merge_calls, [])
        self.c.github.review_results.append({"id": 2, "user": {"login": "reviewer"}, "state": "APPROVED"})
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "merged")

    def test_external_commit_blocks_merge(self):
        self.ready_pr()
        self.approve()
        self.c.github.record["head"]["sha"] = "a" * 40
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "blocked")
        self.assertEqual(self.c.github.merge_calls, [])

    def test_durable_outbox_reconciles_uncertain_post(self):
        original = self.c.jira.post_once
        def uncertain(*args):
            original(*args)
            raise TimeoutError("Response lost after server accepted comment")
        self.c.jira.post_once = uncertain
        with self.assertRaises(TimeoutError):
            self.c.flush()
        count = len(self.c.jira.posts)
        self.c.jira.post_once = original
        self.c.flush()
        self.assertEqual(len(self.c.jira.posts), count)
        self.assertEqual(self.c.store.pending_posts(), [])

    def test_uncertain_merge_is_reconciled_without_second_merge(self):
        self.ready_pr()
        self.approve()
        original = self.c.github.merge
        def uncertain(*args):
            original(*args)
            raise TimeoutError("Response lost after remote merge")
        self.c.github.merge = uncertain
        with self.assertLogs("ticketforge.coordinator", level="ERROR"):
            self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "merging")
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "merged")
        self.assertEqual(len(self.c.github.merge_calls), 1)

    def test_input_during_merge_is_recorded_as_follow_up(self):
        self.ready_pr()
        self.approve()
        original = self.c.github.merge
        def race(*args):
            self.c.jira.say("Also add international shipping.")
            self.c.sync_one(KEY)
            return original(*args)
        self.c.github.merge = race
        self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.store.get(KEY)["state"], "merged")
        self.assertTrue(any("not included" in p["body"] for p in self.c.jira.posts))

    def test_restart_recovers_stage_and_retains_outbox(self):
        t = self.c.store.get(KEY)
        self.c.store.update(KEY, t["version"], "coding", round=1)
        self.c.store.close()
        self.c.store = Store(Path(self.c.cfg.state_dir) / "state.sqlite3")
        self.c.store.recover()
        self.assertEqual(self.c.store.get(KEY)["state"], "ready")
        self.assertGreaterEqual(len(self.c.store.pending_posts()), 2)

    def test_duplicate_poll_does_not_restart_or_duplicate_comments(self):
        self.ready_pr()
        self.c.flush()
        before = copy.deepcopy(self.c.store.get(KEY))
        count = len(self.c.jira.posts)
        for _ in range(3):
            self.c.poll()
            self.c.flush()
        self.assertEqual(self.c.store.get(KEY), before)
        self.assertEqual(len(self.c.jira.posts), count)

    def test_concurrent_input_invalidates_atomic_update(self):
        before = self.c.store.get(KEY)
        self.c.jira.say("Use 150.")
        thread = threading.Thread(target=self.c.sync_one, args=(KEY,))
        thread.start()
        thread.join()
        with self.assertRaises(Superseded):
            self.c.store.update(KEY, before["version"], "awaiting_approval", head="a" * 40)

    def test_manual_merge_completion_is_reported_on_jira(self):
        ticket = self.ready_pr()
        self.c.github.merge(1, ticket["data"]["head"])
        self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.store.get(KEY)["state"], "merged")
        self.assertTrue(any("Merged:" in p["body"] for p in self.c.jira.posts))

    def test_protected_ci_changes_are_blocked_before_commit(self):
        workspace, _ = self.c.repo.prepare(KEY)
        protected = workspace / ".github" / "workflows"
        protected.mkdir(parents=True)
        (protected / "ci.yml").write_text("untrusted workflow change")
        from ticketforge.runtime import ExecutionError
        with self.assertRaisesRegex(ExecutionError, "operator-protected"):
            self.c.repo.commit(workspace, KEY, 1)

    def test_auto_merge_disabled_still_accepts_and_reports_approval(self):
        self.ready_pr()
        self.c.cfg.auto_merge = False
        self.approve()
        self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.store.get(KEY)["state"], "awaiting_approval")
        self.assertEqual(self.c.github.merge_calls, [])
        self.assertTrue(any("Automatic merging is disabled" in p["body"] for p in self.c.jira.posts))

    def test_review_workspace_experiments_do_not_change_coder_files(self):
        ticket = self.ready_pr()
        workspace, _ = self.c.repo.prepare(KEY)
        reviewer = self.c.repo.review_workspace(KEY, workspace, ticket["data"]["head"], ticket["version"])
        original = (workspace / "shipping.py").read_text()
        (reviewer / "shipping.py").write_text("reviewer experiment")
        self.assertEqual((workspace / "shipping.py").read_text(), original)

    def test_pr_announcement_and_link_are_recovered_from_saved_pr(self):
        ticket = self.ready_pr()
        with self.c.store.transaction() as db:
            db.execute("DELETE FROM outbox WHERE body LIKE '%PR raised:%'")
        self.c.store.update(KEY, ticket["version"], jira_linked=None)
        self.c.jira.links.clear()
        self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.jira.links, [ticket["data"]["pr_url"]])
        self.assertTrue(any("PR raised:" in p["body"] for p in self.c.jira.posts))

    def test_status_and_outgoing_message_commit_atomically(self):
        import sqlite3
        ticket = self.c.store.get(KEY)
        with self.c.store.transaction() as db:
            db.execute("CREATE TRIGGER reject_message BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.store.update(KEY, ticket["version"], "coding", notice=("test", "Starting"))
        self.assertEqual(self.c.store.get(KEY)["state"], ticket["state"])

    def test_revised_requirement_produces_passing_checks_on_same_size_source_change(self):
        old = self.ready_pr()
        self.c.jira.say("Use 150 instead of 100.")
        self.c.poll()
        self.c.tick()
        current = self.c.store.get(KEY)
        self.assertEqual(current["state"], "awaiting_approval")
        self.assertNotEqual(current["data"]["head"], old["data"]["head"])
        self.assertTrue(all(v["exit_code"] == 0 for v in current["data"]["verification"]))

    def test_unsupported_repository_is_reported_on_jira_without_model_work(self):
        from ticketforge.project import UnsupportedRepository
        def unsupported(workspace):
            raise UnsupportedRepository("Only a root Go module is supported")
        self.c.repo.context = unsupported
        with self.assertLogs("ticketforge.coordinator", level="ERROR"):
            self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.store.get(KEY)["state"], "blocked")
        self.assertEqual(self.c.agents.calls, [])
        self.assertTrue(any("root Go module" in p["body"] for p in self.c.jira.posts))


if __name__ == "__main__":
    unittest.main()
