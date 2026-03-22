"""Configurable gate strategy engine.

Gates are no longer hardcoded to "pass" — each gate has a configurable
min_status, policy (default/release_only/waivable), and optional waiver.
"""

from .enums import VerifyStatus, status_satisfies
from .models import GateRequirement
from .errors import GateUnsatisfiedError


def check_gate(
    requirement: GateRequirement,
    current_status: VerifyStatus,
    context: str = "default",
) -> tuple[bool, str]:
    """Check if a single gate requirement is satisfied.

    Args:
        requirement: The gate requirement to check.
        current_status: Current verify status of the gate node.
        context: "default" for normal operations, "release" for release gate.

    Returns:
        (satisfied, reason) tuple.
    """
    # release_only gates are skipped in non-release context
    if requirement.policy == "release_only" and context != "release":
        return True, ""

    # waivable gates that have been waived
    if requirement.policy == "waivable" and requirement.waived_by:
        return True, f"waived by {requirement.waived_by}"

    # Failed is always a blocker
    if current_status == VerifyStatus.FAILED:
        return False, f"{requirement.node_id} is FAILED"

    # Check if current status meets the minimum
    min_status = VerifyStatus.from_str(requirement.min_status)
    if not status_satisfies(current_status, min_status):
        return False, (
            f"{requirement.node_id} requires {min_status.value}, "
            f"got {current_status.value}"
        )

    return True, ""


def check_all_gates(
    gates: list[GateRequirement],
    get_status: callable,
    context: str = "default",
) -> tuple[bool, list[dict]]:
    """Check all gates for a node.

    Args:
        gates: List of GateRequirement for the node.
        get_status: Callable(node_id) -> VerifyStatus for looking up gate node status.
        context: "default" or "release".

    Returns:
        (all_satisfied, unsatisfied_list) where unsatisfied_list contains
        dicts with {"node_id", "reason"}.
    """
    unsatisfied = []
    for gate in gates:
        current = get_status(gate.node_id)
        ok, reason = check_gate(gate, current, context)
        if not ok:
            unsatisfied.append({"node_id": gate.node_id, "reason": reason})

    return len(unsatisfied) == 0, unsatisfied


def check_gates_or_raise(
    node_id: str,
    gates: list[GateRequirement],
    get_status: callable,
    context: str = "default",
) -> None:
    """Check all gates and raise GateUnsatisfiedError if any fail."""
    ok, unsatisfied = check_all_gates(gates, get_status, context)
    if not ok:
        raise GateUnsatisfiedError(node_id, unsatisfied)
