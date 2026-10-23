import unittest

from ticketforge import review


def finding(severity="blocking", problem="Boundary charged"):
    return {"severity": severity, "file": "shipping.py", "line": 2, "problem": problem, "evidence": "repro"}


def reviewer(status, findings=(), resolutions=(), dropped=()):
    return {"status": status, "summary": "s", "questions": [], "findings": list(findings),
            "resolutions": list(resolutions), "dropped": list(dropped)}


class ValidationTests(unittest.TestCase):
    def test_verdict_must_match_what_remains_open(self):
        review.check_verdict("pass", False)
        review.check_verdict("changes_requested", True)
        with self.assertRaisesRegex(ValueError, "pass cannot"):
            review.check_verdict("pass", True)
        with self.assertRaisesRegex(ValueError, "needs a blocking"):
            review.check_verdict("changes_requested", False)

    def test_reviewer_result_shape(self):
        cases = [
            ("bad_severity", reviewer("changes_requested", [finding("critical")]), "severity"),
            ("dropped_without_evidence", reviewer("pass", dropped=[{"id": "N1"}]), "dropped"),
            ("negative_line", reviewer("changes_requested", [{**finding(), "line": -1}]), "line"),
            ("extra_field", reviewer("changes_requested", [{**finding(), "fix": "x"}]), "exactly"),
        ]
        for name, args, message in cases:
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                review.validate_finish("reviewer", args)

    def test_valid_results_pass(self):
        review.validate_finish("reviewer", reviewer("pass", [finding("advisory")]))
        review.validate_finish("reviewer", reviewer("changes_requested", [finding()]))
        review.validate_finish("coder", {"status": "ready", "summary": "s", "questions": [],
                                         "responses": [{"id": "F1", "action": "disputed", "evidence": "test proves it"}],
                                         "notes": "Touched shipping.py", "lessons": ["Run make gen after editing protos"]})

    def test_role_fields_are_exact(self):
        with self.assertRaisesRegex(ValueError, "fields must be exactly"):
            review.validate_finish("coder", {"status": "ready", "summary": "s", "questions": [], "findings": []})
        with self.assertRaisesRegex(ValueError, "Unanswered questions"):
            review.validate_finish("assessor", {"status": "ready", "summary": "s", "questions": ["why?"]})

    def test_require_ids_reports_missing_unknown_and_duplicates(self):
        review.require_ids([{"id": "F1"}], ["F1"], "responses")
        for name, items in (("missing", []), ("unknown", [{"id": "F1"}, {"id": "F9"}]), ("duplicate", [{"id": "F1"}, {"id": "F1"}])):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "exactly once"):
                review.require_ids(items, ["F1"], "responses")


class LedgerTests(unittest.TestCase):
    def raise_one(self, ledger, max_disputes=2):
        return review.apply_review(ledger, reviewer("changes_requested", [finding()]), 1, max_disputes)

    def test_new_findings_get_stable_ids_and_advisories_do_not_block(self):
        ledger = review.new_ledger()
        open_items, escalated, advisories = review.apply_review(
            ledger, reviewer("changes_requested", [finding(), finding("advisory", "Naming")]), 1, 2)
        self.assertEqual([i["id"] for i in open_items], ["F1"])
        self.assertEqual([i["id"] for i in advisories], ["F2"])
        self.assertEqual(escalated, [])
        self.assertEqual(ledger["next"], 3)

    def test_fixed_then_resolved_closes_the_finding(self):
        ledger = review.new_ledger()
        self.raise_one(ledger)
        review.record_coder(ledger, [{"id": "F1", "action": "fixed", "evidence": "inclusive now"}], 2)
        open_items, _, _ = review.apply_review(
            ledger, reviewer("pass", resolutions=[{"id": "F1", "status": "resolved", "evidence": "verified"}]), 2, 2)
        self.assertEqual(open_items, [])
        self.assertEqual(ledger["items"]["F1"]["status"], "resolved")
        self.assertEqual([h["actor"] for h in ledger["items"]["F1"]["history"]], ["reviewer", "coder", "reviewer"])

    def test_dispute_accepted_withdraws_finding(self):
        ledger = review.new_ledger()
        self.raise_one(ledger)
        review.record_coder(ledger, [{"id": "F1", "action": "disputed", "evidence": "spec says exclusive"}], 2)
        review.apply_review(ledger, reviewer("pass", resolutions=[{"id": "F1", "status": "withdrawn", "evidence": "agreed"}]), 2, 2)
        self.assertEqual(ledger["items"]["F1"]["status"], "withdrawn")

    def test_repeated_dispute_escalates_at_limit_and_reopens_after_human_input(self):
        ledger = review.new_ledger()
        self.raise_one(ledger, max_disputes=2)
        keep = reviewer("changes_requested", resolutions=[{"id": "F1", "status": "open", "evidence": "still wrong"}])
        for round_number, expect_escalated in ((2, False), (3, True)):
            review.record_coder(ledger, [{"id": "F1", "action": "disputed", "evidence": "not a defect"}], round_number)
            open_items, escalated, _ = review.apply_review(ledger, keep, round_number, 2)
            self.assertEqual(bool(escalated), expect_escalated)
        self.assertEqual(ledger["items"]["F1"]["status"], "escalated")
        self.assertEqual(review.open_findings(ledger), [])
        review.reopen_escalated(ledger, 5)
        self.assertEqual(ledger["items"]["F1"]["status"], "open")
        self.assertEqual(ledger["items"]["F1"]["disputes"], 0)

    def test_open_after_claimed_fix_does_not_count_as_dispute(self):
        ledger = review.new_ledger()
        self.raise_one(ledger, max_disputes=1)
        review.record_coder(ledger, [{"id": "F1", "action": "fixed", "evidence": "changed"}], 2)
        _, escalated, _ = review.apply_review(
            ledger, reviewer("changes_requested", resolutions=[{"id": "F1", "status": "open", "evidence": "not fixed"}]), 2, 1)
        self.assertEqual(escalated, [])
        self.assertEqual(ledger["items"]["F1"]["status"], "open")

    def test_views_separate_what_each_side_sees(self):
        ledger = review.new_ledger()
        self.raise_one(ledger)
        review.record_coder(ledger, [{"id": "F1", "action": "disputed", "evidence": "test X passes"}], 2)
        self.assertEqual(review.for_coder(ledger)[0]["reviewer_latest"], "repro")
        reconcile = review.for_reconcile(ledger)[0]
        self.assertEqual(reconcile["coder_response"], {"action": "disputed", "evidence": "test X passes"})
        self.assertNotIn("summary", reconcile)


if __name__ == "__main__":
    unittest.main()
