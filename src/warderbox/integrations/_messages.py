"""What an adapter tells the model about a blocked or failed call.

The model needs the reason to recover: a budget stop, a rate limit and a risky
query each call for a different next step. Reasons and errors are masked first.
An output violation is the exception, because its details can quote the very
data that was withheld; the caller still gets them from the ActionResult, the
history and the audit log.
"""

from __future__ import annotations

from typing import Any

from warderbox.action import ActionResult

BLOCKED_MESSAGE = "[BLOCKED] Action blocked by WarderBox safety policy"
WITHHELD_MESSAGE = "[BLOCKED] Tool output withheld by WarderBox safety policy"


def model_safe_error(result: ActionResult, shield: Any = None) -> str | None:
    """The message for a blocked or failed result, or None if it succeeded."""
    if result.output_withheld:
        return WITHHELD_MESSAGE
    masker = getattr(shield, "_masker", None)
    if result.blocked:
        # Security-first: the model learns that the action was blocked, not
        # which rule fired. The detailed reason stays in history and the audit
        # log for the operator; exposing it lets a prompt-injected agent probe
        # the policy and look for an adjacent bypass.
        return BLOCKED_MESSAGE
    if result.error:
        # Tool errors still need to reach the agent so it can recover; secrets
        # and PII are masked before they leave the process.
        error = masker.mask_string(result.error) if masker else result.error
        return f"[ERROR] {error}"
    return None
