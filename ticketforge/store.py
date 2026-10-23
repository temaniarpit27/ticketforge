from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path


from .integrations import ASSIGNED

# States in which the agent works or will work on the ticket without a human merge.
AGENT_STATES = ("ready", "assessing", "coding", "reviewing", "publishing", "waiting_input", "blocked")
# States the agent never returns to on its own; such tickets are no longer read from Jira.
FINISHED_STATES = ("merged", "closed", "unassigned")


class Superseded(Exception):
    """The task changed or paused while an agent was running."""


class Store:
    """Everything TicketForge remembers, in one SQLite file. Safe to use from both threads.

    Tables:
      tickets  - one row per Jira ticket: the current requirements and their version number,
                 the state (ready, coding, waiting_input, ...), when it last joined the ready
                 queue (ready_since), and a JSON "data" field with the rest (plan, finding
                 ledger, PR details, counters).
      comments - a fingerprint of every human comment seen, so an edited comment is noticed
                 and a /forge command runs only once.
      events   - a history of what happened, for debugging.
      outbox   - Jira comments waiting to be sent. Saved together with the state change they
                 announce, so a crash can neither lose nor duplicate them.
      meta     - small settings; today only which ticket the worker is focused on.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS tickets (
            key TEXT PRIMARY KEY, version INTEGER NOT NULL, snapshot TEXT NOT NULL,
            state TEXT NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS comments (
            key TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY(key,id));
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, key TEXT, created REAL, kind TEXT, payload TEXT);
          CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, body TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Older databases lack these columns; add them in place.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tickets)")}
        for column in ("ready_since",):
            if column not in columns:
                self.db.execute(f"ALTER TABLE tickets ADD COLUMN {column} REAL")
        # The database itself records when a ticket joined the ready queue, so every code path
        # that makes a ticket ready is covered. The scheduler serves the longest-waiting first.
        now = "((julianday('now') - 2440587.5) * 86400.0)"
        self.db.executescript(f"""
          CREATE TRIGGER IF NOT EXISTS ticket_new AFTER INSERT ON tickets BEGIN
            UPDATE tickets SET ready_since = {now} WHERE key = NEW.key AND NEW.state = 'ready';
          END;
          CREATE TRIGGER IF NOT EXISTS ticket_ready AFTER UPDATE OF state ON tickets
            WHEN NEW.state = 'ready' AND OLD.state != 'ready' BEGIN
            UPDATE tickets SET ready_since = {now} WHERE key = NEW.key;
          END;
        """)
        self.db.commit()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            with self.db:
                yield self.db

    def _event(self, db, key, kind, payload):
        db.execute("INSERT INTO events(key,created,kind,payload) VALUES(?,?,?,?)",
                   (key, time.time(), kind, json.dumps(payload)))

    def _post(self, db, key, identity, body):
        # The identity hashes to a stable outbox id and a marker in the comment text; posting
        # the same identity twice is ignored here, and checked against Jira when sent.
        digest = hashlib.sha256(f"{key}:{identity}".encode()).hexdigest()[:24]
        db.execute("INSERT OR IGNORE INTO outbox(id,key,body) VALUES(?,?,?)",
                   (digest, key, f"TicketForge\n\n{body}\n\n[ticketforge:{digest}]"))

    def post(self, key, identity, body):
        with self.transaction() as db:
            self._post(db, key, identity, body)

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM tickets WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            item["data"] = json.loads(item["data"])
            return item

    def all(self):
        with self.lock:
            keys = [r[0] for r in self.db.execute("SELECT key FROM tickets ORDER BY key")]
        return [self.get(k) for k in keys]

    def sync(self, key, snapshot, comments):
        """Apply a complete authorized Jira snapshot; commands are processed once per edit.

        A changed specification bumps the version, which invalidates any agent run in flight
        (its next guard raises Superseded). Assignment is tracked separately so that
        reassigning a ticket never looks like new requirements.
        """
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
            raise ValueError("Invalid Jira issue key")
        snapshot = dict(snapshot)
        # Trackers without an assignment concept count as assigned.
        assigned = snapshot.pop(ASSIGNED, True)
        encoded = json.dumps(snapshot, sort_keys=True)
        with self.transaction() as db:
            old = self.get(key)
            if old is None and not assigned:
                return
            if old is None:
                db.execute("INSERT INTO tickets(key,version,snapshot,state,data) VALUES(?,?,?,?,?)", (key, 1, encoded, "ready", "{}"))
                self._post(db, key, "accepted", "Ticket accepted. I will investigate and ask questions here when needed.")
                self._event(db, key, "accepted", {"version": 1})
            elif json.dumps(old["snapshot"], sort_keys=True) != encoded:
                version = old["version"] + 1
                data = old["data"]
                # New requirements mean new code: earlier approval of proposed notes no longer applies.
                data.update(approval=None, review=None, round=0, review_round=0, verification_repairs=0, retry_at=0, remote_retries=0,
                            lessons_approved=None)
                state = old["state"] if old["state"] in ("paused", "unassigned", "merged", "closed") else "ready"
                db.execute("UPDATE tickets SET version=?, snapshot=?, state=?, data=? WHERE key=?",
                           (version, encoded, state, json.dumps(data), key))
                msg = ("New input received after this run finished. Create a follow-up ticket for further implementation."
                       if state in ("merged", "closed") else
                       f"New input received. Specification is now v{version}; previous review and approval are invalidated.")
                self._post(db, key, f"input:{version}", msg)
                self._event(db, key, "input", {"version": version})
            for comment in comments:
                cid = str(comment["id"])
                body = comment["text"].strip()
                digest = hashlib.sha256(body.encode()).hexdigest()
                seen = db.execute("SELECT digest FROM comments WHERE key=? AND id=?", (key, cid)).fetchone()
                if seen and seen[0] == digest:
                    continue
                db.execute("INSERT OR REPLACE INTO comments VALUES(?,?,?)", (key, cid, digest))
                if not body.startswith("/forge "):
                    continue
                ticket = self.get(key)
                command = body[len("/forge "):].strip()
                data = ticket["data"]
                state, version = ticket["state"], ticket["version"]
                if command in ("pause", "resume", "retry") and state not in ("merged", "closed"):
                    version += 1
                    state = "paused" if command == "pause" else "ready"
                    data.update(approval=None, review=None, round=0, review_round=0, verification_repairs=0, retry_at=0, remote_retries=0)
                    db.execute("UPDATE tickets SET version=?,state=?,data=? WHERE key=?",
                               (version, state, json.dumps(data), key))
                    self._post(db, key, f"command:{cid}:{digest}", f"Command applied: {command}. Current state: {state}.")
                elif command == "learn" or command.startswith("learn "):
                    lessons = (data.get("coder_result") or {}).get("lessons") or []
                    choice = command[len("learn"):].replace(" ", "")
                    picks = list(range(1, len(lessons) + 1)) if not choice else (
                        [int(n) for n in choice.split(",")] if re.fullmatch(r"[0-9]+(,[0-9]+)*", choice) else None)
                    if state != "awaiting_approval" or not lessons:
                        msg = "There are no proposed notes to keep right now. They are offered with a PR."
                    elif picks is None or any(not 1 <= n <= len(lessons) for n in picks):
                        msg = f"Use /forge learn to keep all notes, or numbers between 1 and {len(lessons)}, for example /forge learn 1,3."
                    else:
                        # Copy the text now: a later round may propose different notes.
                        data["lessons_approved"] = [lessons[n - 1] for n in dict.fromkeys(picks)]
                        db.execute("UPDATE tickets SET data=? WHERE key=?", (json.dumps(data), key))
                        msg = f"Will save {len(data['lessons_approved'])} note(s) to the project guide when the PR is merged."
                    self._post(db, key, f"command:{cid}:{digest}", msg)
                elif command.startswith("note "):
                    self._post(db, key, f"command:{cid}:{digest}", "Note recorded. No new implementation requested.")
                else:
                    self._post(db, key, f"command:{cid}:{digest}", "Commands: /forge pause, /forge resume, /forge retry, /forge note TEXT, /forge learn [numbers]. Approve and merge on GitHub. Write requirements and answers as ordinary comments.")
                self._event(db, key, "command", {"command": command, "comment": cid})
            self._sync_assignment(db, key, assigned)

    def _sync_assignment(self, db, key, assigned):
        """Unassigning stops agent work; reassigning resumes it. An open PR keeps being tracked."""
        ticket = self.get(key)
        state, version, data = ticket["state"], ticket["version"], ticket["data"]
        if not assigned and state in AGENT_STATES:
            target, message = "unassigned", "Ticket is no longer assigned to the agent. Work stopped; saved work is kept. Reassign the ticket to resume."
        elif assigned and state == "unassigned":
            target, message = "ready", "Ticket assigned to the agent again. Resuming against the current specification."
        else:
            return
        # A new version number makes any AI run still going stop at its next check.
        version += 1
        data.update(review=None, round=0, review_round=0, verification_repairs=0, retry_at=0, remote_retries=0)
        db.execute("UPDATE tickets SET version=?,state=?,data=? WHERE key=?", (version, target, json.dumps(data), key))
        self._post(db, key, f"assignment:{version}", message)
        self._event(db, key, target, {"version": version, "assigned": assigned})

    def guard(self, key, version):
        """Stop (raise Superseded) if the ticket changed since this run started: new
        version, paused, unassigned, merged or closed."""
        ticket = self.get(key)
        if ticket["version"] != version or ticket["state"] in ("paused", "unassigned", "merged", "closed"):
            raise Superseded(key)

    def update(self, key, version, state=None, notice=None, **changes):
        """Change state and/or data fields, plus an optional Jira notice, atomically.

        Guarded by version: a write from a superseded run raises instead of overwriting newer
        state. notice is (identity, text); the identity makes the comment idempotent."""
        with self.transaction() as db:
            self.guard(key, version)
            ticket = self.get(key)
            data = ticket["data"]
            data.update(changes)
            target = state or ticket["state"]
            db.execute("UPDATE tickets SET state=?,data=? WHERE key=? AND version=?",
                       (target, json.dumps(data), key, version))
            self._event(db, key, target, {"version": version, "fields": list(changes)})
            if notice:
                self._post(db, key, notice[0], notice[1][:24000])

    def pending_posts(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY rowid")]

    def remote_success(self, key, version):
        # Called after a successful work() or PR poll, so only consecutive failures block.
        """Reset a consecutive failure streak, including successful terminal observations."""
        with self.transaction() as db:
            ticket = self.get(key)
            if ticket["version"] != version:
                raise Superseded(key)
            data = ticket["data"]
            if data.get("remote_retries", 0) or data.get("retry_at", 0):
                data.update(remote_retries=0, retry_at=0)
                db.execute("UPDATE tickets SET data=? WHERE key=? AND version=?",
                           (json.dumps(data), key, version))
                self._event(db, key, "remote_recovered", {"version": version})

    def mark_sent(self, identity):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET sent=1 WHERE id=?", (identity,))

    def recover(self):
        """Single-process startup: restart incomplete stages from persisted workspaces."""
        with self.transaction() as db:
            for t in self.all():
                if t["state"] in ("assessing", "coding", "reviewing", "publishing", "merging"):
                    state = "awaiting_approval" if t["state"] == "merging" else "ready"
                    db.execute("UPDATE tickets SET state=? WHERE key=?", (state, t["key"]))
                    self._post(db, t["key"], f"recovery:{time.time_ns()}", "Service restarted. Reconciling saved work and external state before continuing.")

    def merged(self, key, sha, url):
        """Remote merge is authoritative even if input arrived during the API request."""
        with self.transaction() as db:
            t = self.get(key)
            data = t["data"]
            data.update(merge_sha=sha, approval=None)
            db.execute("UPDATE tickets SET state='merged',data=? WHERE key=?", (json.dumps(data), key))
            self._event(db, key, "merged", {"sha": sha, "version": t["version"]})
            self._post(db, key, "merged", f"Merged: {url}\nMerge commit: {sha}\nThis ticket's run is complete.")
            if t["version"] != data.get("reviewed_version"):
                self._post(db, key, "input-during-merge", "New input arrived at the merge boundary. It was not included in the merged review. Please create a follow-up ticket; the completed merge cannot be retroactively changed.")

    def finish(self, key, state):
        """Move a ticket the agent no longer works on to merged/closed, without a version check."""
        if state not in ("merged", "closed"):
            raise ValueError("finish() only records merged or closed")
        with self.transaction() as db:
            db.execute("UPDATE tickets SET state=? WHERE key=?", (state, key))
            self._event(db, key, state, {"observed": True})

    def annotate(self, key, **changes):
        """Set data fields without a version check. Only for finished (merged/closed) tickets,
        where no agent run can be in progress."""
        with self.transaction() as db:
            data = self.get(key)["data"]
            data.update(changes)
            db.execute("UPDATE tickets SET data=? WHERE key=?", (json.dumps(data), key))
            self._event(db, key, "annotated", {"fields": list(changes)})

    def current(self):
        """The ticket the worker is focused on, kept across restarts (or None)."""
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE name = 'current'").fetchone()
            return row[0] if row else None

    def set_current(self, key):
        """Remember which ticket the worker is focused on (see Coordinator.next_ticket)."""
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('current', ?)", (key,))

    def close(self):
        self.db.close()
