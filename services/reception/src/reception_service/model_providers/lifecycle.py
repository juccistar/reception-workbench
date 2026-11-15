"""Evidence for one model HTTP attempt, owned by its actual runtime task."""
from dataclasses import dataclass


@dataclass
class WireAttempt:
    deadline: float
    entered: bool = False
    status: int | None = None
    retry_after: float = 1.0
