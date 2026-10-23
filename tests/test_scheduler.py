"""Scheduling: one worker, never interrupted, longest-waiting ticket next; and which tickets get read."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from ticketforge.demo import fixture
from ticketforge.store import Store

SPEC = {"summary": "Task", "description": "Do it", "notes": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = fixture(Path(self.tmp.name))
        self.worked, self.checked, self.plan = [], [], {}
        self.c.work = self.fake_work
        self.c.check_merge = lambda ticket: self.checked.append(ticket["key"])

    def tearDown(self):
        self.c.store.close()
        self.tmp.cleanup()

    def fake_work(self, ticket):
        # Each call is one round of work; the plan says which state the ticket ends in.
        self.worked.append(ticket["key"])
        self.c.store.update(ticket["key"], ticket["version"], self.plan[ticket["key"]].pop(0))

    def add(self, key, state="ready", waited_since=None):
        self.c.store.sync(key, SPEC, [])
        with self.c.store.transaction() as db:
            db.execute("UPDATE tickets SET state=? WHERE key=?", (state, key))
            if waited_since is not None:
                db.execute("UPDATE tickets SET ready_since=? WHERE key=?", (waited_since, key))

    def ticks(self, n):
        for _ in range(n):
            self.c.tick()


class SchedulerTests(Base):
    def test_keeps_current_ticket_until_it_cannot_continue(self):
        self.add("APP-1", waited_since=1)
        self.add("APP-2", waited_since=2)
        self.plan = {"APP-1": ["ready", "ready", "waiting_input"], "APP-2": ["waiting_input"]}
        self.ticks(4)
        self.assertEqual(self.worked, ["APP-1", "APP-1", "APP-1", "APP-2"])

    def test_current_ticket_is_kept_even_when_an_older_ticket_becomes_ready(self):
        self.add("APP-1", state="waiting_input")
        self.add("APP-2", waited_since=5)
        self.plan = {"APP-2": ["ready", "ready", "blocked"], "APP-1": ["waiting_input"]}
        self.c.tick()
        # The developer answers APP-1: it joins the queue but does not interrupt APP-2.
        self.c.store.update("APP-1", 1, "ready")
        with self.c.store.transaction() as db:
            db.execute("UPDATE tickets SET ready_since=1 WHERE key='APP-1'")
        self.ticks(3)
        self.assertEqual(self.worked, ["APP-2", "APP-2", "APP-2", "APP-1"])

    def test_next_ticket_is_the_one_waiting_longest_then_lowest_key(self):
        cases = [
            ("oldest_first", {"APP-1": 30, "APP-2": 20, "APP-3": 10}, ["APP-3", "APP-2", "APP-1"]),
            ("tie_by_key", {"APP-2": 10, "APP-1": 10, "APP-3": 5}, ["APP-3", "APP-1", "APP-2"]),
        ]
        for name, waits, expected in cases:
            with self.subTest(name):
                # Fresh state per case; unittest's tearDown cleans up the last one.
                self.tearDown()
                self.setUp()
                for key, since in waits.items():
                    self.add(key, waited_since=since)
                self.plan = {key: ["blocked"] for key in waits}
                self.ticks(3)
                self.assertEqual(self.worked, expected)

    def test_ticket_backing_off_after_rate_limit_lets_others_run(self):
        self.add("APP-1", waited_since=1)
        self.add("APP-2", waited_since=2)
        self.c.store.set_current("APP-1")
        self.c.store.update("APP-1", 1, retry_at=time.time() + 600)
        self.plan = {"APP-2": ["ready", "blocked"], "APP-1": ["blocked"]}
        self.ticks(2)
        self.assertEqual(self.worked, ["APP-2", "APP-2"])
        self.c.store.update("APP-1", 1, retry_at=0)
        self.c.tick()
        self.assertEqual(self.worked[-1], "APP-1")

    def test_waiting_prs_are_still_checked_while_another_ticket_is_worked(self):
        self.add("APP-1", state="awaiting_approval")
        self.add("APP-2")
        self.plan = {"APP-2": ["ready"]}
        self.c.tick()
        self.assertEqual((self.checked, self.worked), (["APP-1"], ["APP-2"]))

    def test_waiting_pr_respects_rate_limit_back_off(self):
        # From the external review: a 600 s back-off at t=1000 must not be checked at t=1121.
        from unittest.mock import patch
        from ticketforge.integrations import RateLimited
        self.add("APP-1", state="awaiting_approval")
        calls = []

        def limited(ticket):
            calls.append(ticket["key"])
            if len(calls) == 1:
                raise RateLimited(600)
        self.c.check_merge = limited
        every = self.c.cfg.status_poll_seconds
        for wall, mono, expected in ((1000, 0, 1), (1000 + every + 1, every + 1, 1), (1601, 700, 2)):
            with patch("ticketforge.coordinator.time.time", return_value=wall), \
                 patch("ticketforge.coordinator.time.monotonic", return_value=mono):
                self.c.tick()
            self.assertEqual(len(calls), expected, (wall, mono))
        self.assertEqual(self.c.store.get("APP-1")["state"], "awaiting_approval")

    def test_back_off_survives_restart(self):
        import time as clock
        self.add("APP-1", state="awaiting_approval")
        self.c.store.update("APP-1", 1, retry_at=clock.time() + 600)
        self.c.status_schedule.clear()  # as after a restart: nothing scheduled in memory
        self.c.tick()
        self.assertEqual(self.checked, [])

    def test_nothing_ready_does_nothing(self):
        self.add("APP-1", state="waiting_input")
        self.add("APP-2", state="awaiting_approval")
        self.c.tick()
        self.assertEqual(self.worked, [])

    def test_current_ticket_survives_a_restart(self):
        self.add("APP-1", waited_since=1)
        self.add("APP-2", waited_since=2)
        self.c.store.set_current("APP-2")
        self.c.store.close()
        self.c.store = Store(Path(self.tmp.name) / "state" / "state.sqlite3")
        self.plan = {"APP-2": ["blocked"], "APP-1": ["blocked"]}
        self.ticks(2)
        self.assertEqual(self.worked, ["APP-2", "APP-1"])


class QueueTimeTests(unittest.TestCase):
    def test_ready_since_is_set_on_every_return_to_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "s.sqlite3")
            store.sync("APP-1", SPEC, [])
            first = store.get("APP-1")["ready_since"]
            self.assertAlmostEqual(first, time.time(), delta=5)
            store.update("APP-1", 1, retry_at=1)
            self.assertEqual(store.get("APP-1")["ready_since"], first)  # still ready: place kept
            store.update("APP-1", 1, "waiting_input")
            time.sleep(0.01)
            store.update("APP-1", 1, "ready")
            self.assertGreater(store.get("APP-1")["ready_since"], first)  # back of the queue
            store.close()

    def test_older_database_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE tickets (key TEXT PRIMARY KEY, version INTEGER NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("""INSERT INTO tickets VALUES ('APP-1', 1, '{}', 'ready', '{}')""")
            db.commit()
            db.close()
            store = Store(path)
            self.assertIsNone(store.get("APP-1")["ready_since"])
            self.assertIsNone(store.current())
            store.sync("APP-2", SPEC, [])
            self.assertIsNotNone(store.get("APP-2")["ready_since"])
            store.close()


class PollTests(Base):
    def test_finished_and_unassigned_tickets_are_not_read_again(self):
        states = {"APP-1": "merged", "APP-2": "closed", "APP-3": "unassigned", "APP-4": "waiting_input",
                  "APP-5": "paused", "APP-6": "awaiting_approval", "APP-7": "ready"}
        for key, state in states.items():
            self.add(key, state)
        read = []
        self.c.sync_one = read.append
        # The filter still matches APP-1 (merged, still assigned) and finds new APP-8 and reassigned APP-3.
        self.c.jira.discover = lambda: ["APP-1", "APP-3", "APP-8"]
        self.c.poll()
        self.assertEqual(sorted(read), ["APP-3", "APP-4", "APP-5", "APP-6", "APP-7", "APP-8"])


if __name__ == "__main__":
    unittest.main()
