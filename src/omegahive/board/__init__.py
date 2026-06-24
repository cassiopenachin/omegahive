"""Board projection: fold the event log into per-task state + transition rules."""

from .reducer import Board, TaskState, fold
from .transitions import validate_transition

__all__ = ["Board", "TaskState", "fold", "validate_transition"]
