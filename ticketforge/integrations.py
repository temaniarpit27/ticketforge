from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from .config import Config, secret


class RemoteError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteError("Unexpected API redirect refused")


class HTTP:
    def __init__(self, base, headers):
        self.base = base.rstrip("/")
        self.headers = headers
        self.opener = urllib.request.build_opener(NoRedirect)

    def call(self, method, path, body=None):
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, raw, method=method,
                                         headers={"Content-Type": "application/json", **self.headers})
        try:
            with self.opener.open(request, timeout=90) as response:
                result = response.read(8 * 1024 * 1024 + 1)
                if len(result) > 8 * 1024 * 1024:
                    raise RemoteError("API response exceeds size limit")
                return json.loads(result) if result else {}
        except urllib.error.HTTPError as exc:
            # Do not echo upstream bodies, which may contain credentials or private data.
            raise RemoteError(f"{method} {urllib.parse.urlsplit(path).path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RemoteError(f"{method} {urllib.parse.urlsplit(path).path}: connection failed; outcome may be uncertain") from None


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


def to_adf(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else
        {"type": "paragraph", "content": []} for line in text.splitlines()
    ]}


class Jira:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        auth = base64.b64encode(f"{secret('JIRA_EMAIL')}:{secret('JIRA_API_TOKEN')}".encode()).decode()
        self.http = HTTP(cfg.jira_url, {"Authorization": "Basic " + auth, "Accept": "application/json"})
        self.bot = self.http.call("GET", "/rest/api/3/myself")["accountId"]
        if self.bot in cfg.allowed_jira_accounts:
            raise ValueError("Use a separate Jira bot account; it cannot be an authorized human")

    def discover(self):
        keys, token = [], None
        while True:
            body = {"jql": self.cfg.jira_jql, "fields": ["summary"], "maxResults": 100}
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
        fields = self.http.call("GET", f"/rest/api/3/issue/{key}?fields=summary,description")["fields"]
        comments = []
        for c in self.comments(key):
            author = c.get("author", {}).get("accountId")
            if author == self.bot or author not in self.cfg.allowed_jira_accounts:
                continue
            comments.append({"id": str(c["id"]), "author": author, "text": adf_text(c.get("body", "")).strip()})
        notes = [c for c in comments if not c["text"].startswith("/forge ")]
        return {"summary": fields.get("summary", ""), "description": adf_text(fields.get("description")),
                "notes": notes}, comments

    def post_once(self, key, identity, body):
        marker = f"[ticketforge:{identity}]"
        # Reconcile before a POST, including after an uncertain previous response.
        if any(c.get("author", {}).get("accountId") == self.bot and marker in adf_text(c.get("body"))
               for c in self.comments(key)):
            return
        self.http.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": to_adf(body)})

    def link(self, key, url):
        self.http.call("POST", f"/rest/api/3/issue/{key}/remotelink", {
            "globalId": "ticketforge:" + url,
            "object": {"url": url, "title": "TicketForge pull request"}})


class GitHub:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = HTTP("https://api.github.com", {
            "Authorization": "Bearer " + secret("GITHUB_TOKEN"),
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
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

    def merge(self, number, sha):
        result = self.http.call("PUT", self.root + f"/pulls/{number}/merge", {"sha": sha, "merge_method": "squash"})
        if not result.get("merged"):
            raise RemoteError("GitHub did not merge; repository rules or branch state may block it")
        return result
