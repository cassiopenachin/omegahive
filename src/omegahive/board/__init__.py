"""Board projection: fold the event log into per-task state + the legality spec."""

from .legality import RULES, LegalityRule, Rejection, lookup, worker_ownership_violation
from .reducer import Board, TaskState, fold

__all__ = [
    "Board", "TaskState", "fold",
    "RULES", "LegalityRule", "Rejection", "lookup", "worker_ownership_violation",
]
