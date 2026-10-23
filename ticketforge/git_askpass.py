#!/usr/bin/env python3
"""Host-only Git credential bridge. Never mounted into agent containers."""
import os
import sys

if "username" in " ".join(sys.argv[1:]).lower():
    print(os.environ.get("TICKETFORGE_GIT_USERNAME", "x-access-token"))
else:
    print(os.environ.get("TICKETFORGE_GIT_PASSWORD", os.environ.get("GITHUB_TOKEN", "")))
