"""A language adapter is a build/verification contract, not just a prompt."""


class LanguageAdapterTemplate:
    def inspect(self, root):
        raise NotImplementedError("Detect supported manifest/toolchain, bounded guides, and skill catalog")

    def dockerfile(self, context, has_sum):
        raise NotImplementedError("Generate a versioned image with dependencies preloaded; no runtime network")

    def verification_commands(self):
        raise NotImplementedError("Return meaningful build/test/format checks for this language")
