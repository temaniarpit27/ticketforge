from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Wire protocols TicketForge can speak. Most vendors and local servers expose one of these.
MODEL_APIS = ("anthropic-messages", "openai-responses", "openai-chat")
AUTH_STYLES = ("bearer", "x-api-key", "api-key", "none")
DEFAULT_AUTH = {"anthropic-messages": "x-api-key", "openai-responses": "bearer", "openai-chat": "bearer"}
# Built-in providers; model_providers in the profile can override these or add more.
BUILTIN_MODEL_PROVIDERS = {
    "anthropic": {"api": "anthropic-messages", "base_url": "https://api.anthropic.com/v1", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"api": "openai-responses", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
}
PROVIDER_FIELDS = {"api", "base_url", "api_key_env", "auth", "headers", "timeout_seconds", "max_tokens_field"}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def provider_specs(cfg):
    """Built-in providers merged with the profile's, each with defaults filled in. Validates."""
    specs = {}
    for name, raw in {**BUILTIN_MODEL_PROVIDERS, **(getattr(cfg, "model_providers", None) or {})}.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError(f"Invalid model provider name {name!r}")
        if not isinstance(raw, dict) or set(raw) - PROVIDER_FIELDS:
            raise ValueError(f"Provider {name}: allowed fields are {', '.join(sorted(PROVIDER_FIELDS))}")
        spec = {"auth": DEFAULT_AUTH.get(raw.get("api")), "headers": {}, "timeout_seconds": 600,
                "max_tokens_field": "max_tokens", "api_key_env": "", **raw}
        if spec.get("api") not in MODEL_APIS:
            raise ValueError(f"Provider {name}: api must be one of {', '.join(MODEL_APIS)}")
        url = spec.get("base_url")
        host = re.match(r"https?://([^/:]+|\[[^\]]+\])", url or "")
        if not isinstance(url, str) or not host or not (url.startswith("https://") or host.group(1) in LOCAL_HOSTS):
            raise ValueError(f"Provider {name}: base_url must use https (plain http only for localhost)")
        if spec["auth"] not in AUTH_STYLES:
            raise ValueError(f"Provider {name}: auth must be one of {', '.join(AUTH_STYLES)}")
        if spec["api_key_env"] and not re.fullmatch(r"[A-Z][A-Z0-9_]*", spec["api_key_env"]):
            raise ValueError(f"Provider {name}: api_key_env must name an environment variable")
        if spec["auth"] != "none" and not spec["api_key_env"]:
            raise ValueError(f"Provider {name}: set api_key_env, or auth \"none\" for an unauthenticated local server")
        if not isinstance(spec["headers"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in spec["headers"].items()):
            raise ValueError(f"Provider {name}: headers must map strings to strings (never put secrets here)")
        if type(spec["timeout_seconds"]) is not int or spec["timeout_seconds"] < 1:
            raise ValueError(f"Provider {name}: timeout_seconds must be a positive integer")
        if spec["max_tokens_field"] not in ("max_tokens", "max_completion_tokens"):
            raise ValueError(f"Provider {name}: max_tokens_field must be max_tokens or max_completion_tokens")
        specs[name] = {**spec, "headers": dict(spec["headers"])}
    return specs


@dataclass
class Config:
    """One profile: target repository, integrations, models per role and every limit.

    Fields map 1:1 to config.json keys. Unknown keys are rejected by the constructor;
    validate() catches bad values at startup rather than mid-ticket. Secrets never live
    here, only the names of the environment variables that hold them."""
    repo: str
    jira_url: str
    jira_jql: str
    allowed_jira_accounts: list[str]
    github_repo: str
    coder_model: str
    reviewer_model: str
    coder_provider: str = "anthropic"
    reviewer_provider: str = "anthropic"
    model_providers: dict = field(default_factory=dict)
    require_agent_assignment: bool = True
    # Extra Jira fields that are part of the requirements, e.g. an acceptance-criteria custom
    # field ("customfield_10034"). A change to any of them restarts the work like a comment.
    jira_spec_fields: list[str] = field(default_factory=list)
    issue_tracker: str = "jira"
    code_host: str = "github"
    language: str = "go"
    tracker_options: dict = field(default_factory=dict)
    code_host_options: dict = field(default_factory=dict)
    commit_author_name: str = ""
    commit_author_email: str = ""
    state_dir: str = ".ticketforge"
    project_guide: str = ""
    base_branch: str = "main"
    sandbox_image: str = "ticketforge-sandbox:local"
    verification_commands: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=lambda: [".github/workflows", ".gitmodules", ".env", ".env.*"])
    auto_merge: bool = False
    poll_seconds: int = 15
    status_poll_seconds: int = 30
    max_agent_steps: int = 30
    max_review_rounds: int = 3
    max_verification_repairs: int = 3
    max_input_chars: int = 180000
    max_output_tokens: int = 6000
    reviewer_max_output_tokens: int = 25000
    max_finding_disputes: int = 2
    command_timeout: int = 300
    sandbox_file_size_mb: int = 1024
    sandbox_tmp_mb: int = 1024
    http_max_attempts: int = 3
    max_remote_retries: int = 3

    @classmethod
    def load(cls, path: str) -> Config:
        source = Path(path).resolve()
        cfg = cls(**json.loads(source.read_text()))
        for name in ("repo", "state_dir"):
            value = Path(getattr(cfg, name)).expanduser()
            setattr(cfg, name, str((source.parent / value).resolve()))
        if cfg.project_guide:
            value = Path(cfg.project_guide).expanduser()
            cfg.project_guide = str((source.parent / value).resolve())
        cfg.validate()
        return cfg

    def validate(self):
        if not (Path(self.repo) / ".git").exists():
            raise ValueError("repo must be an existing Git checkout")
        if not self.jira_url.startswith("https://"):
            raise ValueError("jira_url must use HTTPS")
        if not self.allowed_jira_accounts:
            raise ValueError("Set allowed_jira_accounts to the humans allowed to instruct the agent")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repo):
            raise ValueError("github_repo must be owner/repository")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", self.base_branch) or ".." in self.base_branch:
            raise ValueError("Invalid base_branch")
        if not self.verification_commands:
            raise ValueError("Configure at least one meaningful verification command")
        if self.auto_merge:
            raise ValueError("Automatic merging is unsupported: review and merge manually on GitHub")
        if min(self.poll_seconds, self.status_poll_seconds, self.max_agent_steps, self.max_review_rounds,
               self.max_verification_repairs, self.command_timeout, self.max_input_chars,
               self.max_output_tokens, self.reviewer_max_output_tokens, self.max_finding_disputes,
               self.sandbox_file_size_mb, self.sandbox_tmp_mb, self.http_max_attempts,
               self.max_remote_retries) < 1:
            raise ValueError("Limits must be positive")
        if not isinstance(self.jira_spec_fields, list) or any(
                not isinstance(f, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", f) for f in self.jira_spec_fields):
            raise ValueError("jira_spec_fields must be a list of Jira field IDs such as customfield_10034")
        specs = provider_specs(self)
        for provider in (self.coder_provider, self.reviewer_provider):
            if provider not in specs:
                raise ValueError(f"Unknown model provider {provider!r}; define it in model_providers or use one of: {', '.join(specs)}")
        if self.max_input_chars < 4096:
            raise ValueError("max_input_chars must leave at least 4096 characters for task and tool context")
        if not self.coder_model or not self.reviewer_model:
            raise ValueError("Both model IDs are required")
        if self.project_guide and not Path(self.project_guide).is_file():
            raise ValueError("Configured project_guide does not exist")

    def required_secrets(self):
        specs = provider_specs(self)
        keys = [specs[p]["api_key_env"] for p in (self.coder_provider, self.reviewer_provider)]
        return ["JIRA_EMAIL", "JIRA_API_TOKEN", "GITHUB_TOKEN", *dict.fromkeys(k for k in keys if k)]


def load_env(path: Path):
    """Read simple KEY=value entries without evaluating shell code."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Invalid .env line; use KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value
