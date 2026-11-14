"""Per-evaluation send authorization and content-free diagnostic facts."""
from dataclasses import dataclass, field
from types import MappingProxyType

from .integrations.accio_native.vendor.diagnostics import process_diagnostics


@dataclass(frozen=True)
class SendGateDecision:
    allowed: bool
    reason: str = ''
    facts: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'facts', MappingProxyType(dict(self.facts or {})))

    def diagnostics(self, checkpoint):
        return process_diagnostics({**self.facts, 'gate_reason': self.reason, 'gate_checkpoint': checkpoint})


class SendAuthorization:
    def __init__(self, evaluator):
        self._evaluator = evaluator

    def __call__(self):
        return evaluate_authorization(self, '').allowed is True


def evaluate_authorization(callback, checkpoint):
    try:
        if isinstance(callback, SendAuthorization):
            decision = callback._evaluator()
            if isinstance(decision, SendGateDecision) and type(decision.allowed) is bool:
                return decision
            return SendGateDecision(False, 'guard_evaluation_failed')
        return SendGateDecision(True) if callable(callback) and callback() is True else SendGateDecision(False, 'authorization_rejected')
    except Exception:
        return SendGateDecision(False, 'guard_evaluation_failed')
