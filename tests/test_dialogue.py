import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ticketforge.config import Config
from ticketforge.demo import KEY, fixture
from ticketforge.integrations import Jira, assigned_jql, to_adf
from ticketforge.store import Store, Superseded


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = fixture(Path(self.tmp.name))
        self.bodies = []
        ensure = self.c.github.ensure_pr
        self.c.github.ensure_pr = lambda branch, title, body: (self.bodies.append(body), ensure(branch, title, body))[1]

    def tearDown(self):
        self.c.store.close()
        self.tmp.cleanup()

    def start(self, text="Use a threshold of 100."):
        self.c.jira.say(text)
        self.c.poll()

    def state(self):
        return self.c.store.get(KEY)["state"]

    def ledger(self):
        return self.c.store.get(KEY)["data"]["findings"]["items"]

    def posts(self):
        self.c.flush()
        return "\n".join(p["body"] for p in self.c.jira.posts)


class DialogueTests(Base):
    def test_finding_fixed_by_coder_is_resolved_by_reviewer_reconciliation(self):
        self.start()
        self.c.tick()
        self.assertEqual(self.state(), "ready")
        self.assertEqual(self.ledger()["F1"]["status"], "open")
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        coders = [x for x in self.c.agents.calls if x["role"] == "coder"]
        reviewers = [x for x in self.c.agents.calls if x["role"] == "reviewer"]
        self.assertEqual(coders[0]["task"]["open_findings"], [])
        self.assertEqual([f["id"] for f in coders[1]["task"]["open_findings"]], ["F1"])
        # The reviewer's task never carries earlier findings; they only arrive after its independent pass.
        for call in reviewers:
            self.assertNotIn("open_findings", call["task"])
            self.assertNotIn("prior_findings", call["task"])
        self.assertEqual(self.ledger()["F1"]["status"], "resolved")
        self.assertEqual([h["actor"] for h in self.ledger()["F1"]["history"]], ["reviewer", "coder", "reviewer"])
        self.assertIn("F1 resolved", self.bodies[-1])

    def dispute_forever(self):
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role != "coder" or not task["open_findings"]:
                return original(role, workspace, task, guard, trace, on_finish)
            (workspace / "shipping.py").write_text("def shipping_cost(subtotal):\n    return 0 if subtotal > 100 else 10\n")
            result = {"status": "ready", "summary": "Kept exclusive threshold", "questions": [],
                      "notes": "", "lessons": [],
                      "responses": [{"id": f["id"], "action": "disputed", "evidence": "Ticket says 'above 100'."}
                                    for f in task["open_findings"]]}
            self.assertIsNone(on_finish(result))
            return result
        self.c.agents.run = run
        return original

    def test_repeated_dispute_escalates_to_jira_and_human_answer_restarts_dialogue(self):
        self.start()
        original = self.dispute_forever()
        for _ in range(self.c.cfg.max_finding_disputes + 1):
            self.c.tick()
        self.assertEqual(self.state(), "waiting_input")
        self.assertEqual(self.ledger()["F1"]["status"], "escalated")
        text = self.posts()
        self.assertIn("disagree and need your decision", text)
        self.assertIn("Coder (disputed): Ticket says 'above 100'.", text)
        self.assertIsNone(self.c.github.record)
        self.c.agents.run = original
        self.start("F1: the reviewer is right, 100 itself is free.")
        self.assertEqual(self.state(), "ready")
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        item = self.ledger()["F1"]
        self.assertEqual(item["status"], "resolved")
        self.assertIn("human", [h["actor"] for h in item["history"]])

    def test_coordinator_rejects_coder_result_that_skips_an_open_finding(self):
        self.start()
        self.c.tick()
        original = self.c.agents.run
        seen = []

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role == "coder":
                skipped = {"status": "ready", "summary": "s", "questions": [], "responses": [], "notes": "", "lessons": []}
                with self.assertRaisesRegex(ValueError, "Missing: \\['F1'\\]"):
                    on_finish(skipped)
                seen.append(True)
            return original(role, workspace, task, guard, trace, on_finish)
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(seen, [True])

    def test_advisory_notes_do_not_block_and_appear_in_pr(self):
        self.start("Use a threshold of 150.")
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role != "reviewer":
                return original(role, workspace, task, guard, trace, on_finish)
            result = {"status": "pass", "summary": "Correct", "questions": [], "resolutions": [], "dropped": [], "findings": [
                {"severity": "advisory", "file": "shipping.py", "line": 1, "problem": "Name the threshold constant", "evidence": "magic number"}]}
            self.assertIsNone(on_finish(result))
            return result
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.assertEqual(self.ledger()["F1"]["status"], "advisory")
        self.assertIn("Advisory notes (not blocking)", self.bodies[-1])
        self.assertIn("Name the threshold constant", self.bodies[-1])

    def test_first_reviewer_pass_must_not_resolve_unseen_findings(self):
        self.start()
        self.c.tick()
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role == "reviewer":
                early = {"status": "pass", "summary": "s", "questions": [], "findings": [], "dropped": [],
                         "resolutions": [{"id": "F1", "status": "resolved", "evidence": "guess"}]}
                with self.assertRaisesRegex(ValueError, "first pass is independent"):
                    on_finish(early)
            return original(role, workspace, task, guard, trace, on_finish)
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")


class ReconciliationSafetyTests(Base):
    """Regressions: nothing the reviewer found may silently disappear between its two passes."""

    def scripted_reviewer(self, rounds):
        # rounds: one (first_pass, second_pass) per review; second_pass gets the coordinator's reply.
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role != "reviewer":
                return original(role, workspace, task, guard, trace, on_finish)
            first, second = rounds.pop(0)
            reply = on_finish(first)
            if reply is None:
                return first
            final = second(reply)
            self.assertIsNone(on_finish(final))
            return final
        self.c.agents.run = run

    def result(self, status, findings=(), resolutions=(), dropped=()):
        return {"status": status, "summary": "s", "questions": [], "findings": list(findings),
                "resolutions": list(resolutions), "dropped": list(dropped)}

    def finding(self, severity, problem):
        return {"severity": severity, "file": "shipping.py", "line": 2, "problem": problem, "evidence": "repro"}

    def resolve_all(self, reply, status="resolved"):
        return [{"id": p["id"], "status": status, "evidence": "checked"} for p in reply["prior_findings"]]

    def test_first_pass_finding_left_out_of_second_pass_is_kept(self):
        self.start()
        new_bug = self.finding("blocking", "Negative subtotal is not rejected")
        self.scripted_reviewer([
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged")]), None),
            # The second pass resolves F1 but simply leaves out its own new finding N1.
            (self.result("changes_requested", [new_bug]),
             lambda reply: self.result("changes_requested", resolutions=self.resolve_all(reply))),
        ])
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.state(), "ready")
        self.assertIsNone(self.c.github.record)
        self.assertEqual(self.ledger()["F2"]["problem"], "Negative subtotal is not rejected")
        self.assertEqual(self.ledger()["F2"]["status"], "open")

    def test_pass_that_ignores_a_kept_first_pass_finding_is_rejected(self):
        # The reproduction from the external review: resolve the old finding, omit the new
        # one, and say pass. The coordinator refuses; no PR is raised.
        self.start()
        self.scripted_reviewer([
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged")]), None),
            (self.result("changes_requested", [self.finding("blocking", "Negative subtotal is not rejected")]),
             lambda reply: self.result("pass", resolutions=self.resolve_all(reply))),
        ])
        self.c.tick()
        self.c.tick()
        self.assertIsNone(self.c.github.record)
        self.assertIn("pass cannot leave a blocking finding open", self.c.store.get(KEY)["data"]["error"])

    def test_first_pass_finding_dropped_with_evidence_is_removed(self):
        self.start()
        self.scripted_reviewer([
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged")]), None),
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged again")]),
             lambda reply: self.result("pass", resolutions=self.resolve_all(reply),
                                       dropped=[{"id": "N1", "evidence": "Duplicate of F1, now fixed"}])),
        ])
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.assertNotIn("F2", self.ledger())

    def test_unknown_dropped_id_is_rejected(self):
        self.start()
        self.scripted_reviewer([
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged")]), None),
            (self.result("pass"), lambda reply: self.result("pass", resolutions=self.resolve_all(reply),
                                                           dropped=[{"id": "N7", "evidence": "?"}])),
        ])
        self.c.tick()
        # A real model would get this rejection back as a tool error and correct itself;
        # the scripted reviewer cannot, so the round stops with the reason recorded.
        self.c.tick()
        self.assertEqual(self.state(), "blocked")
        self.assertIn("dropped may only name", self.c.store.get(KEY)["data"]["error"])

    def test_advisory_notes_from_every_round_reach_the_pr_until_withdrawn(self):
        self.start()
        self.scripted_reviewer([
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged"),
                                               self.finding("advisory", "Name the threshold constant")]), None),
            (self.result("pass"), lambda reply: self.result("pass", resolutions=[
                {"id": p["id"], "status": "resolved" if p["severity"] == "blocking" else "open", "evidence": "checked"}
                for p in reply["prior_findings"]])),
        ])
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.assertIn("Name the threshold constant", self.bodies[-1])
        self.assertEqual(self.ledger()["F2"]["status"], "advisory")

    def test_withdrawn_advisory_is_left_out_of_the_pr(self):
        self.start()
        self.scripted_reviewer([
            (self.result("changes_requested", [self.finding("blocking", "Boundary charged"),
                                               self.finding("advisory", "Name the threshold constant")]), None),
            (self.result("pass"), lambda reply: self.result("pass", resolutions=self.resolve_all(reply))),
        ])
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.assertNotIn("Advisory notes", self.bodies[-1])
        self.assertIn("F2 resolved: Name the threshold constant", self.bodies[-1])


class AssignmentTests(Base):
    def test_unassigned_ticket_is_never_picked_up(self):
        self.c.jira.assigned = False
        self.start()
        self.assertIsNone(self.c.store.get(KEY))
        self.c.tick()
        self.assertEqual(self.c.agents.calls, [])

    def test_unassign_stops_work_and_reassign_resumes_without_new_spec_version(self):
        self.start("Use a threshold of 150.")
        version = self.c.store.get(KEY)["version"]
        self.c.jira.assigned = False
        self.c.poll()
        self.assertEqual(self.state(), "unassigned")
        self.c.tick()
        self.assertEqual(self.c.agents.calls, [])
        self.c.jira.assigned = True
        self.c.poll()
        self.assertEqual(self.state(), "ready")
        ticket = self.c.store.get(KEY)
        self.assertNotIn("assigned_to_agent", ticket["snapshot"])
        self.assertEqual(ticket["version"], version + 2)
        text = self.posts()
        self.assertIn("no longer assigned", text)
        self.assertIn("assigned to the agent again", text)
        self.assertEqual(text.count("New input received"), 0)
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")

    def test_unassign_during_model_call_discards_the_run(self):
        self.start("Use a threshold of 150.")
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role == "coder":
                self.c.jira.assigned = False
                self.c.sync_one(KEY)
                with self.assertRaises(Superseded):
                    guard()
            return original(role, workspace, task, guard, trace, on_finish)
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(self.state(), "unassigned")
        self.assertIsNone(self.c.github.record)

    def test_open_pr_keeps_being_tracked_after_unassign(self):
        self.start("Use a threshold of 150.")
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.c.jira.assigned = False
        self.c.poll()
        self.assertEqual(self.state(), "awaiting_approval")
        self.c.github.human_merge(1, self.c.store.get(KEY)["data"]["head"])
        self.c.tick()
        self.assertEqual(self.state(), "merged")


class UnassignedPrTests(Base):
    """From the external review: unassigning must stop work, not stop watching an open PR."""

    def raise_pr_then_unassign_and_comment(self):
        self.start("Use a threshold of 150.")
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.c.jira.assigned = False
        self.c.jira.say("Actually also handle free shipping for members.")
        self.c.poll()
        self.assertEqual(self.state(), "unassigned")
        self.c.status_schedule.clear()

    def test_merge_is_still_detected(self):
        self.raise_pr_then_unassign_and_comment()
        calls = len(self.c.agents.calls)
        self.c.github.human_merge(1, self.c.store.get(KEY)["data"]["head"])
        self.c.tick()
        self.assertEqual(self.state(), "merged")
        self.assertEqual(len(self.c.agents.calls), calls)  # no new work was done
        self.assertIn("Merged:", self.posts())

    def test_close_is_still_detected(self):
        self.raise_pr_then_unassign_and_comment()
        self.c.github.record.update(state="closed")
        self.c.tick()
        self.assertEqual(self.state(), "closed")
        self.assertIn("closed without merging", self.posts())


class UnassignedWatchFailureTests(Base):
    """Second review: an unassigned ticket's PR watcher must save its back-off without
    becoming eligible for coding again."""

    def setUp(self):
        super().setUp()
        self.start("Use a threshold of 150.")
        self.c.tick()
        # The review's scenario: PR raised, ticket unassigned, then a new comment arrives.
        self.c.jira.assigned = False
        self.c.jira.say("Also handle free shipping for members.")
        self.c.poll()
        self.assertEqual(self.state(), "unassigned")
        self.c.status_schedule.clear()
        self.calls = 0

    def failing(self, error):
        def observe(ticket):
            self.calls += 1
            raise error
        self.c.observe_pr = observe

    def tick_at(self, wall, mono):
        from unittest.mock import patch
        with patch("ticketforge.coordinator.time.time", return_value=wall), \
             patch("ticketforge.coordinator.time.monotonic", return_value=mono):
            self.c.tick()

    def test_rate_limit_back_off_is_saved_and_honoured(self):
        from ticketforge.integrations import RateLimited
        self.failing(RateLimited(600))
        self.tick_at(1000, 0)
        data = self.c.store.get(KEY)["data"]
        self.assertEqual((data["remote_retries"], data["retry_at"]), (1, 1600))
        self.tick_at(1121, 1121)  # the review's reproduction: too early
        self.assertEqual(self.calls, 1)
        self.tick_at(1601, 1601)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.state(), "unassigned")  # never re-enabled for coding
        self.assertEqual(self.c.agents.calls[-1]["role"], "reviewer")  # no new agent work

    def test_server_delay_longer_than_an_hour_is_respected(self):
        # Third review: a server asking for 7,200 s was retried after 3,600 s.
        from ticketforge.integrations import RateLimited
        self.failing(RateLimited(7200))
        self.tick_at(1000, 0)
        self.assertEqual(self.c.store.get(KEY)["data"]["retry_at"], 1000 + 7200)
        self.tick_at(1000 + 3601, 3601)
        self.assertEqual(self.calls, 1)
        self.tick_at(1000 + 7201, 7201)
        self.assertEqual(self.calls, 2)

    def test_ordinary_failures_back_off_longer_and_never_block(self):
        from ticketforge.integrations import RemoteError
        self.failing(RemoteError("GitHub down"))
        every = self.c.cfg.status_poll_seconds
        moment = 1000
        for attempt in range(1, self.c.cfg.max_remote_retries + 3):
            self.c.status_schedule.clear()
            self.tick_at(moment, moment)
            data = self.c.store.get(KEY)["data"]
            self.assertEqual(data["remote_retries"], attempt)
            self.assertEqual(data["retry_at"], moment + min(3600, every * 2 ** (attempt - 1)))
            moment = data["retry_at"] + 1
        self.assertEqual(self.state(), "unassigned")
        self.assertEqual(self.posts().count("Checking the PR on GitHub is failing"), 1)

    def test_success_clears_the_back_off_and_restart_keeps_it(self):
        from ticketforge.integrations import RateLimited
        self.failing(RateLimited(600))
        self.tick_at(1000, 0)
        self.c.status_schedule.clear()  # restart: nothing scheduled in memory
        self.tick_at(1100, 1100)
        self.assertEqual(self.calls, 1)  # the saved deadline still holds
        del self.c.observe_pr  # back to the real watcher
        self.tick_at(1601, 1601)
        data = self.c.store.get(KEY)["data"]
        self.assertEqual((data["remote_retries"], data["retry_at"]), (0, 0))

    def test_reassignment_during_a_failing_check_wins(self):
        from ticketforge.integrations import RemoteError

        def observe(ticket):
            self.c.jira.assigned = True
            self.c.sync_one(KEY)  # reassigned while the check was running
            raise RemoteError("GitHub down")
        self.c.observe_pr = observe
        self.tick_at(1000, 0)
        ticket = self.c.store.get(KEY)
        self.assertNotEqual(ticket["state"], "unassigned")
        self.assertFalse(ticket["data"].get("remote_retries"))


class RequirementChangeTests(Base):
    def calls(self, role):
        return [c for c in self.c.agents.calls if c["role"] == role]

    def test_change_after_pr_reworks_with_previous_spec_and_updates_same_pr(self):
        self.start()
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        old_head, old_version = self.c.store.get(KEY)["data"]["head"], self.c.store.get(KEY)["version"]
        self.start("Update the threshold to 150 instead.")
        self.assertEqual(self.state(), "ready")
        self.c.tick()
        ticket = self.c.store.get(KEY)
        self.assertEqual(ticket["state"], "awaiting_approval")
        self.assertNotEqual(ticket["data"]["head"], old_head)
        self.assertEqual(self.c.github.record["number"], 1)
        coder, reviewer = self.calls("coder")[-1]["task"], self.calls("reviewer")[-1]["task"]
        for task in (coder, reviewer):
            previous = task["previous_specification"]
            self.assertEqual(previous["version"], old_version)
            self.assertNotIn("150", str(previous["snapshot"]["notes"]))
        self.assertIn("Specification is now", self.posts())

    def test_description_edit_during_coding_discards_run_and_shows_what_changed(self):
        self.start("Use a threshold of 150.")
        original = self.c.agents.run
        edited = []

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role == "coder" and not edited:
                edited.append(task["specification"]["description"])
                self.c.jira.description = "Charge 12 below the threshold instead of 10."
                self.c.sync_one(KEY)
                guard()
            return original(role, workspace, task, guard, trace, on_finish)
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(self.state(), "ready")
        self.assertIsNone(self.c.github.record)
        self.c.agents.run = original
        self.c.tick()
        task = self.calls("coder")[-1]["task"]
        self.assertIn("12", task["specification"]["description"])
        self.assertEqual(task["previous_specification"]["snapshot"]["description"], edited[0])
        # The plan is redone for the new requirements, not reused from the old version.
        self.assertEqual([c["role"] for c in self.c.agents.calls].count("assessor"), 2)

    def test_pause_and_resume_without_new_requirements_shows_no_change(self):
        self.start("Use a threshold of 150.")
        original = self.c.agents.run

        def run(role, workspace, task, guard, trace, on_finish=None):
            if role == "coder":
                self.c.jira.say("/forge pause")
                self.c.sync_one(KEY)
                guard()
            return original(role, workspace, task, guard, trace, on_finish)
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(self.state(), "paused")
        self.c.agents.run = original
        self.start("/forge resume")
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")
        self.assertNotIn("previous_specification", self.calls("coder")[-1]["task"])


class JiraSpecFieldTests(unittest.TestCase):
    def jira(self, extra, fields):
        jira = Jira.__new__(Jira)
        jira.bot = "bot"
        jira.cfg = SimpleNamespace(allowed_jira_accounts=["human"], require_agent_assignment=True, jira_spec_fields=extra)
        requests = []
        jira.http = SimpleNamespace(call=lambda method, path, *a, **kw: (requests.append(path), {"fields": fields})[1])
        jira.comments = lambda key: []
        return jira, requests

    def test_configured_fields_become_part_of_the_requirements(self):
        criteria = to_adf("Given 150 then free")
        jira, requests = self.jira(["customfield_1", "customfield_2", "labels", "customfield_3"], {
            "summary": "S", "description": None, "assignee": {"accountId": "bot"},
            "customfield_1": criteria, "customfield_2": {"value": "High"}, "labels": ["api", "billing"]})
        snapshot, _ = jira.snapshot("APP-1")
        self.assertIn("customfield_1", requests[0])
        self.assertEqual(snapshot["fields"], {"customfield_1": "Given 150 then free", "customfield_2": "High",
                                              "labels": "api, billing", "customfield_3": ""})

    def test_without_configured_fields_snapshot_shape_is_unchanged(self):
        jira, _ = self.jira([], {"summary": "S", "description": None, "assignee": None})
        snapshot, _ = jira.snapshot("APP-1")
        self.assertNotIn("fields", snapshot)
        self.assertFalse(snapshot["assigned_to_agent"])

    def test_field_change_bumps_version_but_resync_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "s.sqlite3")
            base = {"summary": "S", "description": "D", "notes": [], "fields": {"customfield_1": "A"}}
            store.sync("APP-1", base, [])
            store.sync("APP-1", dict(base), [])
            self.assertEqual(store.get("APP-1")["version"], 1)
            store.sync("APP-1", {**base, "fields": {"customfield_1": "B"}}, [])
            self.assertEqual(store.get("APP-1")["version"], 2)
            store.close()

    def test_bad_field_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".git").mkdir()
            cfg = Config(repo=temp, jira_url="https://jira.example", jira_jql="project = APP", allowed_jira_accounts=["human"],
                         github_repo="org/repo", coder_model="a", reviewer_model="b", verification_commands=["t"],
                         jira_spec_fields=["customfield_1,description"])
            with self.assertRaisesRegex(ValueError, "jira_spec_fields"):
                cfg.validate()


class AssignedJqlTests(unittest.TestCase):
    def test_filter_wraps_query_and_keeps_order(self):
        cases = [
            ("project = APP AND labels = ticketforge ORDER BY created ASC", '(project = APP AND labels = ticketforge) AND assignee = "bot-1" ORDER BY created ASC'),
            ("project = APP order by priority DESC", '(project = APP) AND assignee = "bot-1" order by priority DESC'),
            ("project = APP OR project = WEB", '(project = APP OR project = WEB) AND assignee = "bot-1"'),
        ]
        for jql, expected in cases:
            with self.subTest(jql):
                self.assertEqual(assigned_jql(jql, "bot-1"), expected)

    def test_rejects_unexpected_account_id_and_can_be_disabled(self):
        with self.assertRaises(ValueError):
            assigned_jql("project = APP", 'x" OR 1=1')
        jira = Jira.__new__(Jira)
        jira.bot = "557058:abc"
        jira.cfg = SimpleNamespace(jira_jql="project = APP", require_agent_assignment=False)
        self.assertEqual(jira.discovery_jql(), "project = APP")
        jira.cfg.require_agent_assignment = True
        self.assertEqual(jira.discovery_jql(), '(project = APP) AND assignee = "557058:abc"')


if __name__ == "__main__":
    unittest.main()
