"""The ways the attacker agent ends without a response.

Each one ends the program with a non-zero exit code, which the harness records as `writer_failed` (decision 405). An
empty answer would be read as `no_change` and would hide a provider or configuration problem, so a failure never
answers "no actions".
"""


class AttackerError(Exception):
    """The program could not do its work. `code` is a short stable name, `exit_code` the process exit status."""

    exit_code = 1

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code or type(self).__name__.removesuffix("Error").lower()


class ConfigError(AttackerError):
    """The configuration, the operator's brief or the credential do not load."""

    exit_code = 2


class InputError(AttackerError):
    """The request is not a protocol 1.0 request the runtime can drive."""

    exit_code = 3


class ProviderFailure(AttackerError):
    """A model call failed: transport, authentication, balance, a refusal, a cut-off or an unusable body."""

    exit_code = 4


class OutputError(AttackerError):
    """The answer cannot be carried by the protocol's response form."""

    exit_code = 5
