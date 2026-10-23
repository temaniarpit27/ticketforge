"""Template for e.g. GitLab merge requests; not a functioning GitLab integration."""
import os


class CodeHostTemplate:
    def __init__(self, cfg):
        self.options = cfg.code_host_options

    def git_remote(self):
        return self.options["clone_url"]  # HTTPS URL with no embedded credentials.

    def git_credentials(self):
        return self.options["git_username"], os.environ[self.options["token_env"]]

    def ensure_pr(self, branch, title, body):
        raise NotImplementedError("Reconcile by source branch before creating/updating PR or MR")

    def pr(self, number):
        """Normalize native PR/MR data to the contract in docs/extending.md."""
        raise NotImplementedError

    def checks(self, sha):
        raise NotImplementedError("Return {check_name: normalized_state} for this exact commit")

    def reviews(self, number):
        raise NotImplementedError("Normalize human approval/change-request states")

