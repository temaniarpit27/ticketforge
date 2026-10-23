from __future__ import annotations

import base64
import json
import random
import re
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request

from .config import Config, secret


class RemoteError(Exception):
    pass


class RateLimited(RemoteError):
    def __init__(self, delay):
        self.retry_after = max(1, delay)
        super().__init__(f"Remote rate limit; retry after {self.retry_after:.0f} seconds")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteError("Unexpected API redirect refused")


class HTTP:
    """Small JSON client. Refuses redirects, caps response size, and never copies the
    server's error text into our errors (it may contain private data).

    Reads (GET) and AI calls are retried after short failures, waiting a bit longer each
    time. Other writes are never simply repeated: if the answer was lost, the write may
    already have happened, so callers check first instead."""

    def __init__(self, base, headers, max_attempts=3, sleeper=time.sleep, *, retryable_posts=(), timeout=90):
        self.base = base.rstrip("/")
        self.headers = headers
        self.opener = urllib.request.build_opener(NoRedirect)
        self.max_attempts, self.sleep = max_attempts, sleeper
        # POSTs that are safe to repeat because they change nothing outside (AI calls).
        self.retryable_posts, self.timeout = frozenset(retryable_posts), timeout

    def wait(self, delay, guard):
        while delay > 0:
            guard()
            step = min(delay, 0.1)
            self.sleep(step)
            delay -= step
        guard()

    def call(self, method, path, body=None, *, guard=lambda: None):
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, raw, method=method,
                                         headers={"Content-Type": "application/json", **self.headers})
        retryable_call = method == "GET" or (method == "POST" and path in self.retryable_posts)
        for attempt in range(self.max_attempts if retryable_call else 1):
            guard()
            delay = min(30, 2 ** attempt + random.random())
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    result = response.read(8 * 1024 * 1024 + 1)
                    if len(result) > 8 * 1024 * 1024:
                        raise RemoteError("API response exceeds size limit")
                    return json.loads(result) if result else {}
            except urllib.error.HTTPError as exc:
                retry = exc.code == 429 or 500 <= exc.code <= 599
                headers = exc.headers or {}
                remaining = headers.get("X-RateLimit-Remaining")
                after = headers.get("Retry-After")
                secondary_limit = False
                if self.base == "https://api.github.com" and exc.code in (403, 429):
                    # Bounded classification only; never echo potentially private API bodies.
                    try:
                        message = json.loads(exc.read(65536)).get("message", "").lower()
                    except (ValueError, TypeError, AttributeError):
                        message = ""
                    secondary_limit = bool(after) or "secondary rate limit" in message or "abuse detection" in message
                exc.close()
                if secondary_limit:
                    delay = 60
                if after:
                    try:
                        delay = float(after)
                    except ValueError:
                        try:
                            delay = max(1, parsedate_to_datetime(after).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                if remaining == "0":
                    try:
                        delay = max(delay, float(headers.get("X-RateLimit-Reset", "0")) - time.time())
                    except ValueError:
                        pass
                    raise RateLimited(max(delay, 1)) from None
                if secondary_limit:
                    raise RateLimited(delay if after else 60) from None
                if delay > 30 and retry:
                    raise RateLimited(delay) from None
                if not retryable_call or not retry or attempt + 1 >= self.max_attempts:
                    raise RemoteError(f"{method} {urllib.parse.urlsplit(path).path}: HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError):
                if not retryable_call or attempt + 1 >= self.max_attempts:
                    raise RemoteError(f"{method} {urllib.parse.urlsplit(path).path}: connection failed; outcome may be uncertain") from None
            self.wait(delay, guard)


# Snapshot key reporting whether the ticket is assigned to the agent. It is not part of the
# specification: the store removes it before versioning so reassignment never looks like new input.
ASSIGNED = "assigned_to_agent"


def assigned_jql(jql, account_id):
    """Restrict a JQL filter to issues assigned to the agent, keeping any ORDER BY clause."""
    if not re.fullmatch(r"[A-Za-z0-9:_-]+", account_id):
        raise ValueError("Unexpected Jira account ID format")
    match = re.search(r"\s+ORDER\s+BY\s+.*$", jql, flags=re.IGNORECASE | re.DOTALL)
    query, order = (jql[:match.start()], match.group(0)) if match else (jql, "")
    return f'({query.strip()}) AND assignee = "{account_id}"{order}'


def adf_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_text(v) for v in value)
    if not isinstance(value, dict):
        return ""
    if value.get("type") == "text":
        return value.get("text", "")
    if value.get("type") == "hardBreak":
        return "\n"
    text = adf_text(value.get("content", []))
    return text + ("\n" if value.get("type") in ("paragraph", "heading", "listItem") else "")


def field_text(value):
    """Plain text for any Jira field value: rich text, option, user, list, number or empty."""
    if value is None:
        return ""
    if isinstance(value, dict) and value.get("type") == "doc":
        return adf_text(value).strip()
    if isinstance(value, dict):
        # Select options use "value"; users, versions and components use "name"/"displayName".
        for name in ("value", "name", "displayName"):
            if name in value:
                return str(value[name])
        return json.dumps(value, sort_keys=True)
    if isinstance(value, list):
        return ", ".join(field_text(v) for v in value)
    return str(value)


def to_adf(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else
        {"type": "paragraph", "content": []} for line in text.splitlines()
    ]}


class Jira:
    """Jira Cloud: finds tickets (your JQL, limited to ones assigned to the bot), reads them,
    and posts comments without duplicates. Only comments by allowed_jira_accounts count as
    instructions; the bot's own comments are ignored."""

    def issue_url(self, key):
        return self.cfg.jira_url.rstrip("/") + "/browse/" + key

    def __init__(self, cfg: Config):
        self.cfg = cfg
        auth = base64.b64encode(f"{secret('JIRA_EMAIL')}:{secret('JIRA_API_TOKEN')}".encode()).decode()
        self.http = HTTP(cfg.jira_url, {"Authorization": "Basic " + auth, "Accept": "application/json"}, cfg.http_max_attempts)
        self.bot = self.http.call("GET", "/rest/api/3/myself")["accountId"]
        if self.bot in cfg.allowed_jira_accounts:
            raise ValueError("Use a separate Jira bot account; it cannot be an authorized human")

    def discovery_jql(self):
        if not self.cfg.require_agent_assignment:
            return self.cfg.jira_jql
        return assigned_jql(self.cfg.jira_jql, self.bot)

    def discover(self):
        keys, token = [], None
        jql = self.discovery_jql()
        while True:
            body = {"jql": jql, "fields": ["summary"], "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            page = self.http.call("POST", "/rest/api/3/search/jql", body)
            keys.extend(i["key"] for i in page["issues"])
            token = page.get("nextPageToken")
            if not token:
                return keys

    def comments(self, key):
        values, start = [], 0
        while True:
            page = self.http.call("GET", f"/rest/api/3/issue/{key}/comment?startAt={start}&maxResults=100&orderBy=created")
            batch = page.get("comments", [])
            values.extend(batch)
            start += len(batch)
            if start >= page.get("total", start) or not batch:
                return values

    def snapshot(self, key):
        extra = list(getattr(self.cfg, "jira_spec_fields", None) or [])
        names = ",".join(["summary", "description", "assignee", *extra])
        fields = self.http.call("GET", f"/rest/api/3/issue/{key}?fields={names}")["fields"]
        assignee = (fields.get("assignee") or {}).get("accountId")
        comments = []
        for c in self.comments(key):
            author = c.get("author", {}).get("accountId")
            if author == self.bot or author not in self.cfg.allowed_jira_accounts:
                continue
            comments.append({"id": str(c["id"]), "author": author, "text": adf_text(c.get("body", "")).strip()})
        notes = [c for c in comments if not c["text"].startswith("/forge ")]
        snapshot = {"summary": fields.get("summary", ""), "description": adf_text(fields.get("description")),
                    "notes": notes,
                    ASSIGNED: assignee == self.bot or not self.cfg.require_agent_assignment}
        if extra:
            # Only added when configured, so upgrading never makes existing tickets look changed.
            snapshot["fields"] = {name: field_text(fields.get(name)) for name in extra}
        return snapshot, comments

    def post_once(self, key, identity, body):
        marker = f"[ticketforge:{identity}]"
        # Check first whether this comment is already there (an earlier send may have worked
        # even though we never saw the answer), so it is never posted twice.
        if any(c.get("author", {}).get("accountId") == self.bot and marker in adf_text(c.get("body"))
               for c in self.comments(key)):
            return
        self.http.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": to_adf(body)})

    def link(self, key, url):
        self.http.call("POST", f"/rest/api/3/issue/{key}/remotelink", {
            "globalId": "ticketforge:" + url,
            "object": {"url": url, "title": "TicketForge pull request"}})


class GitHub:
    """GitHub code host: PRs, checks and reviews, observed only. There is no merge method."""

    def git_remote(self):
        return f"https://github.com/{self.cfg.github_repo}.git"

    def git_credentials(self):
        return "x-access-token", secret("GITHUB_TOKEN")

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = HTTP("https://api.github.com", {
            "Authorization": "Bearer " + secret("GITHUB_TOKEN"),
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, cfg.http_max_attempts)
        self.root = "/repos/" + cfg.github_repo

    def pages(self, path, field=None):
        result, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            data = self.http.call("GET", path + f"{sep}per_page=100&page={page}")
            batch = data[field] if field else data
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def ensure_pr(self, branch, title, body):
        head = urllib.parse.quote(self.cfg.github_repo.split("/")[0] + ":" + branch, safe="")
        # state=all avoids duplicating a PR after a crash following creation or merge.
        existing = self.pages(self.root + "/pulls?state=all&head=" + head)
        if existing:
            pr = existing[0]
            if pr["state"] != "open":
                raise RemoteError("The ticket branch already has a closed PR; reconcile it before retrying")
            self.http.call("PATCH", self.root + f"/pulls/{pr['number']}", {"title": title, "body": body})
            return pr
        return self.http.call("POST", self.root + "/pulls", {
            "title": title, "body": body, "head": branch, "base": self.cfg.base_branch})

    def pr(self, number):
        return self.http.call("GET", self.root + f"/pulls/{number}")

    def checks(self, sha):
        runs = self.pages(self.root + f"/commits/{sha}/check-runs", "check_runs")
        statuses = self.pages(self.root + f"/commits/{sha}/statuses")
        # Newest record wins; never let an older success mask a newer failure.
        latest = {}
        for run in sorted(runs, key=lambda x: x["id"], reverse=True):
            latest.setdefault(run["name"], "success" if run.get("status") == "completed" and
                              run.get("conclusion") == "success" else run.get("conclusion") or run.get("status"))
        for status in sorted(statuses, key=lambda x: x["id"], reverse=True):
            latest.setdefault(status["context"], status["state"])
        return latest

    def reviews(self, number):
        return self.pages(self.root + f"/pulls/{number}/reviews")
