class GateError(Exception):
    """The run could not be completed. This fails the job as an error, not as a regression."""


class InfraError(GateError):
    """Ollama, Postgres or the network is down or broken."""


class DatasetError(GateError):
    """A case is broken, for example its gold SQL does not run."""
