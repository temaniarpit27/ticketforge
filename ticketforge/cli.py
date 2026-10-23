from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
from pathlib import Path

from .config import Config, load_env, secret
from .coordinator import Coordinator
from .integrations import GitHub, Jira
from .runtime import Agents, Repository, Sandbox
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="TicketForge — Jira-first coding and review")
    parser.add_argument("command", choices=["init", "serve", "doctor", "status", "demo"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--directory", help="Output directory for an integration profile or parent directory for an offline demo")
    parser.add_argument("--repo", help="Existing Go repository to onboard")
    parser.add_argument("--github-repo", help="owner/repository; inferred from origin when possible")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "init":
        from .project import init_profile
        if not args.repo:
            parser.error("init requires --repo /path/to/go-repository")
        directory = Path(args.directory) if args.directory else Path("profiles") / Path(args.repo).expanduser().resolve().name
        cfg, context = init_profile(Path(args.repo), directory, args.github_repo)
        print(f"Created Go integration profile: {directory.resolve()}")
        print(f"Module: {context['module']}; toolchain: Go {context['toolchain_version']}")
        print("Review config.json, copy .env.example to .env, and fill in credentials locally.")
        print("Fill PROJECT.md with architecture and product facts. It is supplied to both agents without modifying the target repository.")
        print(f"Build image with Dockerfile.sandbox and the target repository as its build context. Image: {cfg.sandbox_image}")
        return
    if args.command == "demo":
        from .demo import run_demo
        run_demo(Path(args.directory or ".ticketforge/demos"))
        return
    path = Path(args.config).resolve()
    load_env(path.parent / ".env")
    cfg = Config.load(str(path))
    root = Path(cfg.state_dir)
    root.mkdir(parents=True, exist_ok=True)
    if args.command == "status":
        store = Store(root / "state.sqlite3")
        try:
            print(json.dumps([{k: t[k] for k in ("key", "version", "state", "data")} for t in store.all()], indent=2))
        finally:
            store.close()
        return
    from .project import inspect_project
    context = inspect_project(Path(cfg.repo)) if args.command == "doctor" else None
    for name in ("JIRA_EMAIL", "JIRA_API_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        secret(name)
    sandbox = Sandbox(cfg)
    sandbox.check()
    if args.command == "doctor":
        print("Local configuration, credential presence, Docker engine, and sandbox image checks passed.")
        print(f"Go repository: {context['module']}; declared Go {context['go_version']}")
        print("Credentials and remote permissions have not been validated. No remote changes were made.")
        return
    with (root / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another TicketForge process is using this state directory")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler(root / "service.log")])
        store = Store(root / "state.sqlite3")
        coordinator = Coordinator(cfg, store, Jira(cfg), GitHub(cfg), Repository(cfg), Agents(cfg, sandbox), sandbox)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: coordinator.stop.set())
        try:
            coordinator.run()
        finally:
            # Daemon poll thread may still be completing a timed-out request at shutdown.
            # SQLite is left for process teardown rather than closed under that thread.
            logging.info("TicketForge stopped; saved work will be reconciled on restart")


if __name__ == "__main__":
    main()
