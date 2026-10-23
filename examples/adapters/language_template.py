"""A language adapter is a build/verification contract, not just a prompt."""


class LanguageAdapterTemplate:
    def inspect(self, root):
        raise NotImplementedError("Detect supported manifest/toolchain, bounded guides, and skill catalog")

    def dockerfile(self, context, has_sum):
        raise NotImplementedError("Generate a versioned image with dependencies preloaded; no runtime network")

    def verification_commands(self):
        raise NotImplementedError("Return meaningful build/test/format checks for this language")

    def repo_map_command(self):
        raise NotImplementedError("Shell command writing a package/module listing to /cache")

    def parse_repo_map(self, text, module):
        raise NotImplementedError("Return {module, packages: [{package, doc, files, tests, imports}], truncated}")
