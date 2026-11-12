"""Smart Build: a fair, machine-wide queue for heavy builds."""

__version__ = "0.3.0"

from .queue import Lease, QueueError, WaitTimeout, resolve_project  # noqa: E402

__all__ = ["Lease", "QueueError", "WaitTimeout", "resolve_project", "__version__"]
