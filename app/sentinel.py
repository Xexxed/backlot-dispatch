"""Sentinel agent — pure signal evaluators; agents NEVER mutate schedules.

This module grows in two stages:
  * now: role criticality (shared by the crew responder's Exceptions queue
    and the sentinel's ack-staleness signal);
  * stage 4: the signal evaluators (daylight burn-down, meal-penalty clock,
    ack staleness, imminent weather, slippage) plus the idempotent tick.

Every signal returns a ``Signal`` that may carry an ``Incident``. The web
layer materializes incidents through the standard sandbox pipeline — the
sentinel itself only ever FILES reports, it never writes plan state.
"""
from __future__ import annotations

# Roles whose absence/unavailability halts a unit; matched case-insensitively
# against CrewMember.role (exact values from the imported crew CSV).
CRITICAL_ROLES = frozenset(
    {"dp", "1st ad", "sound mixer", "key grip", "script supervisor"}
)


def is_critical_role(role: str | None) -> bool:
    """True when the role is operationally critical (DP, 1st AD, ...)."""
    if not role:
        return False
    return role.strip().lower() in CRITICAL_ROLES
