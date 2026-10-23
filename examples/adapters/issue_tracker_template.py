"""Template only: implement authenticated API operations and contract tests before use.

For GitHub Issues, map local GH-123 to configured owner/repository issue #123.
For GitLab Issues, map GL-123 to project issue IID 123. Never put URLs in task keys.
"""
import os


class IssueTrackerTemplate:
    def __init__(self, cfg):
        self.options = cfg.tracker_options
        self.token = os.environ[self.options["token_env"]]

    def discover(self):
        """Return stable safe local keys matching [A-Z][A-Z0-9_]*-[0-9]+.

        Only issues opted in for the agent (for example, assigned to the bot account)."""
        raise NotImplementedError("Fetch opted-in issues with pagination")

    def snapshot(self, key):
        """Return (snapshot, authorized_comments).

        snapshot = {summary: str, description: str, notes: [{id, author, text}],
                    assigned_to_agent: bool (optional; omit if the tracker has no assignees)}
        comments includes authorized command comments too; notes excludes /forge commands.
        IDs/authors are strings. Return the full current snapshot, not only deltas.
        Exclude bot comments and unauthorized authors; preserve edits and deletions.
        """
        raise NotImplementedError("Read issue and all comment pages; enforce author allowlist")

    def post_once(self, key, identity, body):
        raise NotImplementedError("Reconcile a bot-owned stable marker before posting")

    def link(self, key, url):
        raise NotImplementedError("Idempotently link the PR/MR from the issue")

    def issue_url(self, key):
        raise NotImplementedError("Return the provider-native issue URL")
