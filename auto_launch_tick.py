"""Opt-in scheduler-facing launch tick, deliberately disabled until wired.

No network access or Amazon mutation occurs when disabled. The production
scheduler must never supply a live flag as a substitute for server opt-in.
"""
import os
from auto_launch_coordinator import LaunchBlocked

def launch_tick(*, coordinator_factory=None, inputs_factory=None):
    if os.getenv("AUTO_LAUNCH_ENABLED", "false").lower() != "true":
        return {"status": "disabled", "created": 0}
    if coordinator_factory is None or inputs_factory is None:
        raise LaunchBlocked("Production launch adapters not configured")
    args = inputs_factory()
    if not isinstance(args, dict):
        raise LaunchBlocked("Missing verified launch inputs")
    coordinator = coordinator_factory()
    if not getattr(coordinator, "enable", False):
        raise LaunchBlocked("Coordinator not explicitly enabled")
    return coordinator.run_one(**args)
