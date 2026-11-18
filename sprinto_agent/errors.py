"""Error taxonomy shared by every integration and tool.

Kinds:
  transient      - safe to retry (network blip, 5xx, rate limit)
  permanent      - will not succeed if repeated unchanged
  auth           - credentials/permissions; stop that integration, surface setup issue
  invalid_input  - bad arguments; never retry unchanged
  quota          - provider quota exhausted; pause affected work, show reason
  setup_required - integration not configured (never fake success)
  uncertain      - irreversible action may or may not have happened (timeout after request sent)
"""
from __future__ import annotations


class ErrorKind:
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AUTH = "auth"
    INVALID_INPUT = "invalid_input"
    QUOTA = "quota"
    SETUP_REQUIRED = "setup_required"
    UNCERTAIN = "uncertain"


class IntegrationError(Exception):
    def __init__(self, kind: str, message: str, integration: str = ""):
        super().__init__(f"[{integration}:{kind}] {message}")
        self.kind = kind
        self.message = message
        self.integration = integration


class OperationUncertain(Exception):
    """An irreversible action's outcome could not be determined; caller must pause."""

    def __init__(self, op_id: str, message: str):
        super().__init__(message)
        self.op_id = op_id


class DeliveryPolicyViolation(Exception):
    """Raised at the integration boundary when a recipient/attendee is not the demo address."""
