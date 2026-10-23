"""How the coder and reviewer talk to each other.

Every problem the reviewer finds becomes a "finding" with an ID (F1, F2, ...). All findings
live in a "ledger": a list saved with the ticket, so nothing is lost between rounds or when
the requirements change. Each round:
  - the coder answers every open finding: "fixed", or "disputed" with proof;
  - the reviewer marks each one "resolved", "withdrawn" (coder was right) or still "open";
  - a finding the two keep disagreeing on is "escalated" to the human on Jira.
This module holds those rules as plain functions (no network, no files), so they are easy
to test on their own.
"""
from __future__ import annotations

SEVERITIES = ("blocking", "advisory")
CODER_ACTIONS = ("fixed", "disputed")
RESOLUTIONS = ("resolved", "open", "withdrawn")

FINDING_SCHEMA = {"type": "object", "properties": {
    "severity": {"type": "string", "enum": list(SEVERITIES)},
    "file": {"type": "string"}, "line": {"type": "integer", "minimum": 0},
    "problem": {"type": "string"}, "evidence": {"type": "string"},
}, "required": ["severity", "file", "line", "problem", "evidence"], "additionalProperties": False}

RESPONSE_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "action": {"type": "string", "enum": list(CODER_ACTIONS)},
    "evidence": {"type": "string"},
}, "required": ["id", "action", "evidence"], "additionalProperties": False}

# A first-pass finding the reviewer takes back in its second pass (retracted, or a duplicate
# of an earlier finding). Anything not listed here is kept: omission never drops a finding.
DROPPED_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "evidence": {"type": "string"},
}, "required": ["id", "evidence"], "additionalProperties": False}

RESOLUTION_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "status": {"type": "string", "enum": list(RESOLUTIONS)},
    "evidence": {"type": "string"},
}, "required": ["id", "status", "evidence"], "additionalProperties": False}

MAX_NOTES = 4000
MAX_LESSONS = 5
MAX_LESSON_CHARS = 300

STATUSES = {
    "assessor": ["ready", "needs_input", "blocked"],
    "coder": ["ready", "needs_input", "blocked"],
    "reviewer": ["pass", "changes_requested", "needs_input", "blocked"],
}

RECONCILE_INSTRUCTIONS = (
    "Your independent pass is recorded; its findings are listed with ids N1, N2, ... and are all KEPT. "
    "Now reconcile earlier findings from previous review rounds (blocking ones and advisory notes). "
    "For each prior finding, inspect the current code and the coder's response, then call finish again with "
    "one resolution per prior finding id: resolved (fixed in this commit, or for a note: no longer applies), "
    "withdrawn (the coder's evidence shows it was not a defect), or open (still applies; for a blocking finding "
    "explain why the coder's evidence does not hold). "
    "To take back one of your own N findings, list it in dropped with evidence, for example 'duplicate of F2'; "
    "leaving it out does not drop it. Use findings only for additional new problems found in this pass. "
    "Human decisions in the specification notes override both sides. If the requirements changed "
    "(see previous_specification), withdraw findings that no longer apply.")


def finish_schema(role):
    properties = {"status": {"type": "string", "enum": STATUSES[role]},
                  "summary": {"type": "string"},
                  "questions": {"type": "array", "items": {"type": "string"}}}
    if role == "coder":
        properties["responses"] = {"type": "array", "items": RESPONSE_SCHEMA}
        # Notes to itself for the next round of this ticket (key files, decisions, what is left).
        properties["notes"] = {"type": "string", "maxLength": MAX_NOTES}
        # Facts about the repository worth keeping for future tickets; a human approves them.
        properties["lessons"] = {"type": "array", "maxItems": MAX_LESSONS,
                                 "items": {"type": "string", "maxLength": MAX_LESSON_CHARS}}
    if role == "reviewer":
        properties["findings"] = {"type": "array", "items": FINDING_SCHEMA}
        properties["resolutions"] = {"type": "array", "items": RESOLUTION_SCHEMA}
        properties["dropped"] = {"type": "array", "items": DROPPED_SCHEMA}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _check_object(value, schema, label):
    if not isinstance(value, dict) or set(value) != set(schema["required"]):
        raise ValueError(f"{label} must have exactly: {', '.join(schema['required'])}")
    for name, spec in schema["properties"].items():
        item = value[name]
        if spec["type"] == "integer":
            if type(item) is not int or item < spec.get("minimum", item):
                raise ValueError(f"{label}.{name} must be a non-negative integer")
        elif not isinstance(item, str) or ("enum" in spec and item not in spec["enum"]):
            raise ValueError(f"{label}.{name} is invalid")


def validate_finish(role, args):
    """Check a finish result has the right shape and does not contradict itself
    (for example, "pass" while still listing a blocking problem).

    Which finding IDs must be answered depends on the ticket, so the caller checks that."""
    schema = finish_schema(role)
    if not isinstance(args, dict) or set(args) != set(schema["required"]):
        raise ValueError("Invalid finish result: fields must be exactly " + ", ".join(schema["required"]))
    status = args["status"]
    if status not in STATUSES[role] or not isinstance(args["summary"], str):
        raise ValueError("Invalid finish status or summary")
    for name in schema["required"]:
        if name in ("status", "summary", "notes"):
            continue
        if not isinstance(args[name], list):
            raise ValueError(f"{name} must be a list")
    if any(not isinstance(q, str) for q in args["questions"]):
        raise ValueError("questions must be strings")
    if role == "coder":
        if not isinstance(args["notes"], str) or len(args["notes"]) > MAX_NOTES:
            raise ValueError(f"notes must be text of at most {MAX_NOTES} characters")
        lessons = args["lessons"]
        if len(lessons) > MAX_LESSONS or any(not isinstance(l, str) or not l.strip() or len(l) > MAX_LESSON_CHARS for l in lessons):
            raise ValueError(f"lessons: at most {MAX_LESSONS} non-empty items of at most {MAX_LESSON_CHARS} characters")
    for item in args.get("responses", []):
        _check_object(item, RESPONSE_SCHEMA, "response")
    for item in args.get("findings", []):
        _check_object(item, FINDING_SCHEMA, "finding")
    for item in args.get("resolutions", []):
        _check_object(item, RESOLUTION_SCHEMA, "resolution")
    for item in args.get("dropped", []):
        _check_object(item, DROPPED_SCHEMA, "dropped")
    if status == "needs_input" and not args["questions"]:
        raise ValueError("needs_input requires concrete questions")
    if status in ("ready", "pass", "changes_requested") and args["questions"]:
        raise ValueError("Unanswered questions require needs_input")
    return args


def check_verdict(status, blocking):
    """The reviewer's own word must match what remains: changes_requested only when something
    blocking is still open, pass only when nothing is. Checked by the coordinator on the
    combined result of both review passes."""
    if status == "pass" and blocking:
        raise ValueError("pass cannot leave a blocking finding open (including kept first-pass findings)")
    if status == "changes_requested" and not blocking:
        raise ValueError("changes_requested needs a blocking finding that is still open")


def require_ids(items, expected, label):
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        missing, extra = sorted(set(expected) - set(ids)), sorted(set(ids) - set(expected))
        raise ValueError(f"{label} must cover each id exactly once. Missing: {missing or 'none'}; unknown: {extra or 'none'}")


def new_ledger():
    return {"next": 1, "items": {}}


def _ordered(ledger, statuses):
    # IDs are allocated in order; sort numerically so output is stable.
    items = [i for i in ledger["items"].values() if i["status"] in statuses]
    return sorted(items, key=lambda i: int(i["id"][1:]))


def open_findings(ledger):
    return _ordered(ledger, ("open",))


def escalated_findings(ledger):
    return _ordered(ledger, ("escalated",))


def for_coder(ledger):
    """Open blocking findings with the reviewer's latest reasoning, for the coder to fix or dispute."""
    return [{"id": i["id"], "file": i["file"], "line": i["line"], "problem": i["problem"],
             "evidence": i["evidence"], "reviewer_latest": i.get("reviewer_latest", "")} for i in open_findings(ledger)]


def advisory_findings(ledger):
    return _ordered(ledger, ("advisory",))


def for_reconcile(ledger):
    """Findings still in force (open blocking ones and advisory notes) with the coder's
    per-finding evidence, for the reviewer's second pass. Never includes the coder's summary."""
    return [{"id": i["id"], "severity": i["severity"], "file": i["file"], "line": i["line"], "problem": i["problem"],
             "evidence": i["evidence"], "coder_response": i.get("coder_latest")}
            for i in _ordered(ledger, ("open", "advisory"))]


def record_coder(ledger, responses, round_number):
    for response in responses:
        item = ledger["items"][response["id"]]
        item["coder_latest"] = {"action": response["action"], "evidence": response["evidence"]}
        item["history"].append({"round": round_number, "actor": "coder", **item["coder_latest"]})


def reopen_escalated(ledger, version):
    """A human answered on Jira: escalated findings go back to both agents with fresh dispute counts."""
    for item in escalated_findings(ledger):
        item.update(status="open", disputes=0)
        item["history"].append({"round": None, "actor": "human", "action": "input", "evidence": f"specification v{version}"})


def apply_review(ledger, review, round_number, max_disputes):
    """Record the reviewer's verdict. Returns (open blocking, newly escalated, advisory notes).

    Advisory notes are every note still in force, from any round, so none silently
    disappears from the PR; the reviewer resolves or withdraws notes that no longer apply."""
    escalated = []
    for resolution in review["resolutions"]:
        item = ledger["items"][resolution["id"]]
        item["reviewer_latest"] = resolution["evidence"]
        item["history"].append({"round": round_number, "actor": "reviewer", "action": resolution["status"],
                                "evidence": resolution["evidence"]})
        if resolution["status"] != "open":
            item["status"] = resolution["status"]
            continue
        if item["status"] == "advisory":
            continue  # still applies; a note never blocks or escalates
        if (item.get("coder_latest") or {}).get("action") == "disputed":
            item["disputes"] += 1
        if item["disputes"] >= max_disputes:
            item["status"] = "escalated"
            escalated.append(item)
    for finding in review["findings"]:
        identity = f"F{ledger['next']}"
        ledger["next"] += 1
        status = "open" if finding["severity"] == "blocking" else "advisory"
        item = {"id": identity, **finding, "status": status, "disputes": 0, "reviewer_latest": finding["evidence"],
                "history": [{"round": round_number, "actor": "reviewer", "action": "raised", "evidence": finding["evidence"]}]}
        ledger["items"][identity] = item
    return open_findings(ledger), escalated, advisory_findings(ledger)


def describe(item):
    text = f"{item['id']} [{item['severity']}] {item['file']}:{item['line']} - {item['problem']}\nEvidence: {item['evidence']}"
    if item.get("reviewer_latest") and item["reviewer_latest"] != item["evidence"]:
        text += f"\nReviewer (latest): {item['reviewer_latest']}"
    if item.get("coder_latest"):
        text += f"\nCoder ({item['coder_latest']['action']}): {item['coder_latest']['evidence']}"
    return text
