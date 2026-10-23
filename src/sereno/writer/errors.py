"""The ways the writer program ends without a response.

Each one ends the program with a non-zero exit code, which the hook records as writer_failed. An empty answer would be
read as no_change and end the run of failures, so the writer never answers "no actions" for a problem it had.
"""


class WriterError(Exception):
    """The writer could not do its work. `code` is a short stable name, `exit_code` the process exit status."""

    exit_code = 1

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code or type(self).__name__.removesuffix("Error").lower()


class ConfigError(WriterError):
    """The configuration, the credential or the campaign's limits do not match what the design froze."""

    exit_code = 2


class InputError(WriterError):
    """The request or the state cannot be turned into a permitted view: a source outside the allowlist, a gap in the
    chain, an input over the size cap, a receipt that does not match what was staged."""

    exit_code = 3


class ProviderFailure(WriterError):
    """The provider call failed: transport, authentication, balance, a refusal, a cut-off or an unusable body."""

    exit_code = 4


class OutputError(WriterError):
    """The provider answered, but not with anything the hook contract can carry, also after the repairs."""

    exit_code = 5
