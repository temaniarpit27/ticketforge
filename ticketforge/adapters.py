"""Explicit extension contracts. Repository instructions cannot register host plugins."""
from typing import Protocol, runtime_checkable


@runtime_checkable
class IssueTracker(Protocol):
    def discover(self): ...
    def snapshot(self, key): ...
    def post_once(self, key, identity, body): ...
    def link(self, key, url): ...
    def issue_url(self, key): ...


@runtime_checkable
class CodeHost(Protocol):
    def git_remote(self): ...
    def git_credentials(self): ...
    def ensure_pr(self, branch, title, body): ...
    def pr(self, number): ...
    def checks(self, sha): ...
    def reviews(self, number): ...


@runtime_checkable
class LanguageAdapter(Protocol):
    def inspect(self, root): ...
    def dockerfile(self, context, has_sum): ...
    def verification_commands(self): ...


class GoAdapter:
    def inspect(self, root):
        from .project import inspect_project
        return inspect_project(root)

    def dockerfile(self, context, has_sum):
        from .project import go_dockerfile
        return go_dockerfile(context, has_sum)

    def verification_commands(self):
        return ['tf_unformatted=$(gofmt -l .) && if [ -n "$tf_unformatted" ]; then printf \'%s\\n\' "$tf_unformatted"; exit 1; fi',
                "go build ./...", "go test ./...", "go test -race ./...", "go vet ./..."]


LANGUAGES = {"go": GoAdapter}
TRACKERS = {}
CODE_HOSTS = {}


def register_tracker(name, factory):
    if name == "jira" or name in TRACKERS:
        raise ValueError("Tracker is already registered")
    TRACKERS[name] = factory


def register_code_host(name, factory):
    if name == "github" or name in CODE_HOSTS:
        raise ValueError("Code host is already registered")
    CODE_HOSTS[name] = factory


def language_adapter(name):
    if name not in LANGUAGES:
        raise ValueError(f"Language adapter {name!r} is not installed; this release supports Go")
    return LANGUAGES[name]()


def create_integrations(cfg):
    # Factories are explicit trusted application code, never imports from a target repo.
    from .integrations import Jira, GitHub
    trackers, hosts = {"jira": Jira, **TRACKERS}, {"github": GitHub, **CODE_HOSTS}
    if cfg.issue_tracker not in trackers or cfg.code_host not in hosts:
        raise ValueError("Requested integration is not installed. This release supports Jira Cloud + GitHub; see docs/extending.md")
    language_adapter(cfg.language)
    return trackers[cfg.issue_tracker](cfg), hosts[cfg.code_host](cfg)
