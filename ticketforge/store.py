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


class Superseded(Exception):
    """The task changed or paused while an agent was running."""


class Store:
    """SQLite state for one profile, safe for the worker and the Jira thread to share.

    tickets: one row per issue with its specification snapshot, version, state and a JSON
    data blob (assessment, findings ledger, PR info, counters). comments: digests of seen
    human comments, so edits are detected and commands run once. events: audit history.
    outbox: Jira comments waiting to be sent, written in the same transaction as the state
    change they announce.
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
        """)

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
                db.execute("INSERT INTO tickets VALUES(?,?,?,?,?)", (key, 1, encoded, "ready", "{}"))
                self._post(db, key, "accepted", "Ticket accepted. I will investigate and ask questions here when needed.")
                self._event(db, key, "accepted", {"version": 1})
            elif json.dumps(old["snapshot"], sort_keys=True) != encoded:
                version = old["version"] + 1
                data = old["data"]
                data.update(approval=None, review=None, round=0, review_round=0, verification_repairs=0, retry_at=0, remote_retries=0)
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
                elif command.startswith("note "):
                    self._post(db, key, f"command:{cid}:{digest}", "Note recorded. No new implementation requested.")
                else:
                    self._post(db, key, f"command:{cid}:{digest}", "Commands: /forge pause, /forge resume, /forge retry, /forge note TEXT. Approve and merge on GitHub. Write requirements and answers as ordinary comments.")
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

    def close(self):
        self.db.close()
