from collections.abc import Sequence


class IterationNodeError(ValueError):
    """Base class for iteration node errors."""


class IteratorVariableNotFoundError(IterationNodeError):
    """Raised when the iterator variable is not found."""


class InvalidIteratorValueError(IterationNodeError):
    """Raised when the iterator value is invalid."""


class StartNodeIdNotFoundError(IterationNodeError):
    """Raised when the start node ID is not found."""


class IterationGraphNotFoundError(IterationNodeError):
    """Raised when the iteration graph is not found."""


class IterationIndexNotFoundError(IterationNodeError):
    """Raised when the iteration index is not found."""


class ChildGraphAbortedError(IterationNodeError):
    """Raised when a child graph aborts and the container must stop immediately."""


class ParallelIterationWaitTimeoutError(IterationNodeError):
    """Raised when the parallel iteration main Future wait exceeds its budget."""

    def __init__(self, pending_indexes: Sequence[int], timeout_seconds: float) -> None:
        self.pending_indexes = list(pending_indexes)
        self.timeout_seconds = timeout_seconds
        super().__init__(
            "parallel iteration wait timed out after "
            f"{timeout_seconds}s; pending indexes: {self.pending_indexes}"
        )


class ParallelSiblingDrainTimeoutError(IterationNodeError):
    """Raised when sibling drain exceeds its configured budget."""

    def __init__(self, pending_indexes: Sequence[int], timeout_seconds: float) -> None:
        self.pending_indexes = list(pending_indexes)
        self.timeout_seconds = timeout_seconds
        super().__init__(
            "parallel sibling drain timed out after "
            f"{timeout_seconds}s; pending indexes: {self.pending_indexes}"
        )
