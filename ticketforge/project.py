"""Repository discovery and reusable Go-repository onboarding, without model guesses."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class UnsupportedRepository(ValueError):
    pass


def safe_text(root: Path, relative: str, limit=6000):
    path = root / relative
    if path.is_symlink() or not path.is_file():
        return None
    if root.resolve() not in path.resolve().parents:
        return None
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    return {"path": relative, "text": raw[:limit].decode("utf-8", errors="replace"), "truncated": len(raw) > limit}


def inspect_project(root: Path):
    root = root.resolve()
    manifest = safe_text(root, "go.mod", 24000)
    if not manifest:
        detected = [label for name, label in (("package.json", "Node/JavaScript"), ("pyproject.toml", "Python"),
                                              ("Cargo.toml", "Rust"), ("go.work", "Go workspace")) if (root / name).exists()]
        raise UnsupportedRepository("This version supports one Go module at the repository root (go.mod required). " +
                                    ("Detected: " + ", ".join(detected) + ". " if detected else "Language could not be established. ") +
                                    "No implementation attempted. Use a supported Go repository; other languages and multi-module workspaces need a future adapter.")
    if manifest["truncated"]:
        raise UnsupportedRepository("go.mod exceeds discovery limit; inspect and configure this repository manually")
    if (root / "go.work").exists():
        raise UnsupportedRepository("Go workspaces are not supported yet. Start with a standalone single-module Go repository.")
    text = manifest["text"]
    module = re.search(r"(?m)^\s*module\s+([^\s]+)", text)
    version = re.search(r"(?m)^\s*go\s+(\d+\.\d+(?:\.\d+)?)\s*(?://.*)?$", text)
    toolchain = re.search(r"(?m)^\s*toolchain\s+go(\d+\.\d+(?:\.\d+)?)\s*(?://.*)?$", text)
    if not module or not version:
        raise UnsupportedRepository("go.mod must declare a module and Go version before automatic onboarding")
    if re.search(r"=>\s+(?:\.{1,2}/|/)", text):
        raise UnsupportedRepository("Local Go module replacements need a custom build context; automatic onboarding currently supports standalone modules only")
    docs = [value for name in ("README.md", "AGENTS.md", "AGENT.md", "agent.md", "CLAUDE.md", "TICKETFORGE.md", "CONTRIBUTING.md",
                               "docs/architecture.md", "docs/ARCHITECTURE.md")
            for value in [safe_text(root, name)] if value]
    entries = []
    for item in sorted(root.iterdir(), key=lambda p: p.name):
        if item.name.startswith(".") or item.is_symlink():
            continue
        entries.append(item.name + ("/" if item.is_dir() else ""))
        if item.is_dir() and item.name not in ("vendor", "node_modules"):
            for child in sorted(item.iterdir(), key=lambda p: p.name)[:20]:
                if not child.name.startswith(".") and not child.is_symlink():
                    entries.append(f"{item.name}/{child.name}" + ("/" if child.is_dir() else ""))
        if len(entries) >= 150:
            break
    warnings = []
    if not any(d["path"] in ("AGENTS.md", "AGENT.md", "agent.md", "CLAUDE.md", "TICKETFORGE.md") for d in docs):
        warnings.append("No dedicated project guide. Investigate source and tests; ask on Jira for material product decisions.")
    return {"language": "go", "module": module[1].strip('"'), "go_version": version[1],
            "toolchain_version": toolchain[1] if toolchain else version[1], "manifest": manifest,
            "documents": docs, "skills": discover_skills(root), "structure": entries[:150], "warnings": warnings,
            "policy": "Discovery is a starting map, not proof of behavior. Inspect relevant code and applicable nested instructions before editing. Missing product context must be clarified on Jira."}


def discover_skills(root):
    catalog = []
    for directory in (".agents/skills", ".claude/skills"):
        parent = root / directory
        if not parent.is_dir() or parent.is_symlink() or root not in parent.resolve().parents:
            continue
        for entry in sorted(parent.iterdir(), key=lambda p: p.name):
            if len(catalog) >= 24:
                return catalog
            if entry.is_symlink() or not entry.is_dir():
                continue
            relative = f"{directory}/{entry.name}/SKILL.md"
            content = safe_text(root, relative, 4000)
            if not content:
                continue
            description = re.search(r"(?m)^description:\s*(.+)$", content["text"])
            catalog.append({"name": entry.name, "path": relative,
                            "description": description[1].strip(' \"\'')[:500] if description else "Read this skill when relevant to the task."})
    return catalog


# Writes the package list to the sandbox cache folder instead of stdout, because command
# output shown to us is capped and a big repository's list would be cut off mid-JSON.
REPO_MAP_FILE = "repo-map.json"
REPO_MAP_COMMAND = ("go list -e -json=ImportPath,Name,Doc,GoFiles,TestGoFiles,XTestGoFiles,Imports ./... "
                    f"> /cache/{REPO_MAP_FILE}")


def parse_repo_map(text, module, limit=30000):
    """Turn `go list -json` output into a compact package map the agents can read at a glance.

    One entry per package: its folder relative to the module ("." is the root), the first
    line of its doc comment, its Go files and test files, and which of this module's own
    packages it imports. If the map is too big, file names are dropped first, then packages.
    """
    decoder, packages, position = json.JSONDecoder(), [], 0
    while True:
        # go list prints one JSON object after another, not a JSON array.
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        item, position = decoder.raw_decode(text, position)

        def local(path):
            return "." if path == module else path[len(module) + 1:] if path.startswith(module + "/") else None

        name = local(item.get("ImportPath", ""))
        if name is None:
            continue
        packages.append({
            "package": name,
            "doc": (item.get("Doc") or "").split("\n")[0][:160],
            "files": item.get("GoFiles") or [],
            "tests": (item.get("TestGoFiles") or []) + (item.get("XTestGoFiles") or []),
            "imports": sorted(p for p in (local(i) for i in item.get("Imports") or []) if p is not None),
        })
    packages.sort(key=lambda p: p["package"])
    result = {"module": module, "packages": packages, "truncated": False}
    if len(json.dumps(result)) > limit:
        for package in packages:
            package["files"], package["tests"] = len(package["files"]), len(package["tests"])
    while len(json.dumps(result)) > limit and packages:
        packages.pop()
        result["truncated"] = True
    return result


def go_dockerfile(context, has_sum):
    copied = "go.mod go.sum" if has_sum else "go.mod"
    return (f"ARG GO_VERSION={context['toolchain_version']}\nFROM golang:${{GO_VERSION}}\n"
            "ENV GOMODCACHE=/opt/ticketforge-modcache\nWORKDIR /opt/ticketforge-deps\n"
            f"COPY {copied} ./\n"
            "RUN mkdir -p /opt/ticketforge-modcache && go mod download && chmod -R a+rX /opt/ticketforge-modcache\n"
            "ENV GOCACHE=/tmp/go-build GOPATH=/tmp/gopath GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOFLAGS=-mod=readonly\n"
            "WORKDIR /workspace\nCMD [\"sh\"]\n")


def guide_template(context):
    return (f"# Project guide for TicketForge\n\nGo module: `{context['module']}`\n"
            f"Declared Go version: `{context['go_version']}`\n\n"
            "Fill in the sections below with facts. This guide supplements source and tests; it is not a security policy.\n\n"
            "## Purpose and product rules\nDescribe what the service does and the invariants changes must preserve.\n\n"
            "## Architecture\nIdentify entry points, important packages, dependencies, storage, and external services.\n\n"
            "## Build and verification\nRecord test commands, required fixtures/services, and meaningful acceptance checks.\n\n"
            "## Conventions and boundaries\nDescribe error handling, logging, compatibility requirements, generated files, and protected areas.\n\n"
            "## When to ask on Jira\nList product decisions that require a human. Do not guess undocumented business rules.\n")


def init_profile(repo: Path, directory: Path, github_repo=None):
    """Create an external integration profile; never modify the target repository."""
    repo, directory = repo.expanduser().resolve(), directory.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError("--repo must point to an existing Git checkout")
    context = inspect_project(repo)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Profile directory is not empty; refusing to overwrite configuration")
    if not github_repo:
        result = subprocess.run(["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True, timeout=10)
        remote = result.stdout.strip()
        match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", remote)
        if match:
            github_repo = match[1]
        else:
            raise ValueError("Cannot infer a GitHub repository from origin; provide --github-repo owner/repository")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repo):
        raise ValueError("Invalid --github-repo")
    result = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                            capture_output=True, text=True, timeout=10)
    base_branch = result.stdout.strip().removeprefix("origin/") if result.returncode == 0 else "main"
    from .config import Config
    from .adapters import GoAdapter
    from dataclasses import asdict
    cfg = Config(repo=str(repo), jira_url="https://your-company.atlassian.net",
                 jira_jql="project = APP AND labels = ticketforge ORDER BY created ASC",
                 allowed_jira_accounts=["replace-with-your-human-jira-account-id"], github_repo=github_repo,
                 coder_model="replace-with-a-model-id-from-coder_provider", reviewer_model="replace-with-a-model-id-from-reviewer_provider",
                 base_branch=base_branch, sandbox_image="ticketforge-" + re.sub(r"[^a-z0-9_.-]", "-", repo.name.lower()) + ":local",
                 project_guide="PROJECT.md",
                 verification_commands=GoAdapter().verification_commands(),
                 required_checks=["replace-with-your-github-ci-check-name"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    (directory / "Dockerfile.sandbox").write_text(go_dockerfile(context, (repo / "go.sum").is_file()))
    (directory / "PROJECT.md").write_text(guide_template(context))
    (directory / "discovered-context.json").write_text(json.dumps(context, indent=2) + "\n")
    (directory / ".env.example").write_text("JIRA_EMAIL=\nJIRA_API_TOKEN=\nGITHUB_TOKEN=\nANTHROPIC_API_KEY=\n"
                                            "# Only needed when a role uses the openai provider.\nOPENAI_API_KEY=\n")
    return cfg, context
