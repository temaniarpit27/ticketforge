from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    repo: str
    jira_url: str
    jira_jql: str
    allowed_jira_accounts: list[str]
    github_repo: str
    coder_model: str
    reviewer_model: str
    state_dir: str = ".ticketforge"
    project_guide: str = ""
    base_branch: str = "main"
    sandbox_image: str = "ticketforge-sandbox:local"
    verification_commands: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=lambda: [".github/workflows", ".gitmodules", ".env", ".env.*"])
    auto_merge: bool = False
    poll_seconds: int = 15
    max_agent_steps: int = 30
    max_review_rounds: int = 3
    max_input_chars: int = 180000
    max_output_tokens: int = 6000
    command_timeout: int = 120

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
            raise ValueError("Set allowed_jira_accounts to the humans allowed to instruct and approve")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repo):
            raise ValueError("github_repo must be owner/repository")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", self.base_branch) or ".." in self.base_branch:
            raise ValueError("Invalid base_branch")
        if not self.verification_commands:
            raise ValueError("Configure at least one meaningful verification command")
        if self.auto_merge and not self.required_checks:
            raise ValueError("auto_merge requires explicitly named required_checks")
        if min(self.poll_seconds, self.max_agent_steps, self.max_review_rounds,
               self.command_timeout, self.max_input_chars, self.max_output_tokens) < 1:
            raise ValueError("Limits must be positive")
        if not self.coder_model or not self.reviewer_model:
            raise ValueError("Both model IDs are required")
        if self.project_guide and not Path(self.project_guide).is_file():
            raise ValueError("Configured project_guide does not exist")


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
