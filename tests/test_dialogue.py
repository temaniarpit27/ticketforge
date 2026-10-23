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
            result = {"status": "pass", "summary": "Correct", "questions": [], "resolutions": [], "findings": [
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
                early = {"status": "pass", "summary": "s", "questions": [], "findings": [],
                         "resolutions": [{"id": "F1", "status": "resolved", "evidence": "guess"}]}
                with self.assertRaisesRegex(ValueError, "first pass is independent"):
                    on_finish(early)
            return original(role, workspace, task, guard, trace, on_finish)
        self.c.agents.run = run
        self.c.tick()
        self.assertEqual(self.state(), "awaiting_approval")


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
