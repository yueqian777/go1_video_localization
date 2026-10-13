class PipelineUnavailable(RuntimeError):
    """The real vision backend is not installed or cannot be loaded."""


class PipelineInputError(ValueError):
    """A user input cannot be safely or geometrically processed."""
