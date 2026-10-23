#!/usr/bin/env python3
"""Host-only Git credential bridge. Never mounted into agent containers."""
import os
import sys

if "username" in " ".join(sys.argv[1:]).lower():
    print("x-access-token")
else:
    print(os.environ.get("GITHUB_TOKEN", ""))
