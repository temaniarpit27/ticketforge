import copy
import tempfile
import threading
import unittest
from unittest.mock import patch
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
        head = self.c.store.get(KEY)["data"]["head"]
        self.c.github.human_merge(1, head)
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
        self.c.sandbox.run = lambda *a, **kw: {"exit_code": 1, "output": "meaningful failing check"}
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "ready")
        self.assertIsNone(self.c.github.record)
        self.assertEqual([x["role"] for x in self.c.agents.calls], ["assessor", "coder"])
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

    def test_ci_and_reviews_are_reported_but_only_a_human_merges(self):
        self.ready_pr()
        self.c.github.check_results = {"tests": "in_progress"}
        self.c.tick()
        self.assertEqual(self.c.github.merge_calls, [])
        self.c.github.check_results = {"tests": "success"}
        self.c.github.review_results = [{"id": 1, "user": {"login": "human"}, "state": "APPROVED"}]
        self.c.status_schedule.clear()
        self.c.tick()
        self.assertEqual(self.c.github.merge_calls, [])
        self.assertEqual(self.c.store.get(KEY)["state"], "awaiting_approval")
        head = self.c.store.get(KEY)["data"]["head"]
        self.c.github.human_merge(1, head)
        self.c.status_schedule.clear()
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

    def test_no_merge_api_exists_on_the_production_code_host(self):
        from ticketforge.integrations import GitHub
        self.assertFalse(hasattr(GitHub, "merge"))
        self.ready_pr()
        self.c.tick()
        self.assertEqual(self.c.github.merge_calls, [])

    def test_input_after_human_merge_is_recorded_as_follow_up(self):
        self.ready_pr()
        head = self.c.store.get(KEY)["data"]["head"]
        self.c.github.human_merge(1, head)
        self.c.jira.say("Also add international shipping.")
        self.c.poll()
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
        self.c.github.human_merge(1, ticket["data"]["head"])
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

    def test_jira_approval_command_cannot_approve_or_merge(self):
        self.ready_pr()
        self.approve()
        self.c.tick()
        self.c.flush()
        self.assertEqual(self.c.store.get(KEY)["state"], "awaiting_approval")
        self.assertEqual(self.c.github.merge_calls, [])
        self.assertIsNone(self.c.store.get(KEY)["data"].get("approval"))
        self.assertTrue(any("Approve and merge on GitHub" in p["body"] for p in self.c.jira.posts))

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

    def test_waiting_pr_is_polled_only_on_its_schedule(self):
        self.ready_pr()
        with patch.object(self.c.github, "pr", wraps=self.c.github.pr) as remote:
            with patch("ticketforge.coordinator.time.monotonic", return_value=100):
                for _ in range(5): self.c.tick()
                self.assertEqual(remote.call_count, 1)
            with patch("ticketforge.coordinator.time.monotonic", return_value=131):
                self.c.tick()
                self.assertEqual(remote.call_count, 2)
            self.c.github.human_merge(1, self.c.store.get(KEY)["data"]["head"])
            with patch("ticketforge.coordinator.time.monotonic", return_value=162):
                self.c.tick()
            self.assertEqual(self.c.store.get(KEY)["state"], "merged")

    def test_verification_repairs_do_not_consume_review_rounds(self):
        self.c.cfg.max_review_rounds = 1
        self.c.jira.say("Use 150.")
        self.c.poll()
        original = self.c.sandbox.run
        count = [0]
        def flaky(*args, **kwargs):
            count[0] += 1
            return {"exit_code": 1, "output": "reproducible failed check"} if count[0] <= 2 else original(*args, **kwargs)
        self.c.sandbox.run = flaky
        self.c.tick(); self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["data"].get("review_round", 0), 0)
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "awaiting_approval")
        self.assertEqual(self.c.store.get(KEY)["data"]["review_round"], 1)

    def test_nested_environment_file_cannot_be_committed(self):
        workspace, _ = self.c.repo.prepare(KEY)
        path = workspace / "cmd" / "api" / ".env.production"
        path.parent.mkdir(parents=True)
        path.write_text("PLACEHOLDER=not-a-real-secret")
        from ticketforge.runtime import ExecutionError
        with self.assertRaisesRegex(ExecutionError, "operator-protected"):
            self.c.repo.commit(workspace, KEY, 1)

    def test_note_does_not_restart_a_waiting_pr(self):
        before = self.ready_pr()
        self.c.jira.say("/forge note I will review tomorrow.")
        self.c.poll()
        after = self.c.store.get(KEY)
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["state"], "awaiting_approval")
        self.c.flush()
        self.assertTrue(any("No new implementation requested" in p["body"] for p in self.c.jira.posts))

    def test_assessment_question_stops_before_coding(self):
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "waiting_input")
        self.assertEqual([c["role"] for c in self.c.agents.calls], ["assessor"])

    def test_remote_retry_defers_and_eventually_blocks_saved_work(self):
        from ticketforge.integrations import RateLimited
        self.ready_pr()
        def limited(ticket):
            raise RateLimited(60)
        self.c.check_merge = limited
        with patch("ticketforge.coordinator.time.time", return_value=100):
            self.c.tick()
            self.assertEqual(self.c.store.get(KEY)["data"]["retry_at"], 160)
            self.c.tick()
            self.assertEqual(self.c.store.get(KEY)["data"]["remote_retries"], 1)
        for i in range(self.c.cfg.max_remote_retries):
            self.c.status_schedule.clear()
            due = self.c.store.get(KEY)["data"]["retry_at"] + 1
            with patch("ticketforge.coordinator.time.time", return_value=due):
                self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "blocked")
        self.c.flush()
        self.assertTrue(any("Retry budget exhausted" in p["body"] for p in self.c.jira.posts))

    def test_successful_pr_poll_resets_errors_across_many_separated_blips(self):
        from ticketforge.integrations import RemoteError
        self.ready_pr()
        for i in range(self.c.cfg.max_remote_retries + 2):
            self.c.status_schedule = {}
            with patch("ticketforge.coordinator.time.time", return_value=100 + i * 100):
                with patch.object(self.c, "check_merge", side_effect=RemoteError("temporary")):
                    self.c.tick()
                self.assertEqual(self.c.store.get(KEY)["data"]["remote_retries"], 1)
            self.c.status_schedule = {}
            with patch("ticketforge.coordinator.time.time", return_value=131 + i * 100):
                self.c.tick()
            current = self.c.store.get(KEY)
            self.assertEqual(current["state"], "awaiting_approval")
            self.assertEqual(current["data"]["remote_retries"], 0)
            self.assertEqual(current["data"]["retry_at"], 0)
        head = current["data"]["head"]
        self.c.github.human_merge(1, head)
        self.c.status_schedule = {}
        self.c.tick()
        self.assertEqual(self.c.store.get(KEY)["state"], "merged")

    def test_successful_work_resets_remote_streak(self):
        ticket = self.c.store.get(KEY)
        self.c.store.update(KEY, ticket["version"], remote_retries=2, retry_at=1)
        self.c.tick()
        current = self.c.store.get(KEY)
        self.assertEqual(current["state"], "waiting_input")
        self.assertEqual(current["data"]["remote_retries"], 0)
        self.assertEqual(current["data"]["retry_at"], 0)

    def test_recovery_reset_does_not_overwrite_new_specification_or_terminal_state(self):
        ticket = self.ready_pr()
        self.c.store.update(KEY, ticket["version"], remote_retries=2, retry_at=1)
        self.c.github.human_merge(1, ticket["data"]["head"])
        self.c.tick()
        current = self.c.store.get(KEY)
        self.assertEqual(current["state"], "merged")
        self.assertEqual(current["data"]["remote_retries"], 0)
        with self.assertRaises(Superseded):
            self.c.store.remote_success(KEY, ticket["version"] - 1)

    def test_target_commits_use_configured_author(self):
        self.c.cfg.commit_author_name = "Configured Author"
        self.c.cfg.commit_author_email = "configured@example.invalid"
        ticket = self.ready_pr()
        workspace, _ = self.c.repo.prepare(KEY)
        self.assertEqual(self.c.repo.git("show", "-s", "--format=%an <%ae>", ticket["data"]["head"], cwd=workspace),
                         "Configured Author <configured@example.invalid>")
        self.assertIn("Generated by TicketForge", self.c.repo.git("show", "-s", "--format=%B", cwd=workspace))


if __name__ == "__main__":
    unittest.main()
