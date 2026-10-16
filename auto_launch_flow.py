"""Fail-closed end-to-end launch runner with explicit resumable states.

This is not connected to the live scheduler. All non-idempotent Amazon POSTs
are executed only by existing guarded adapters, under the account lock.
"""
from auto_launch_coordinator import LaunchBlocked

def continue_launch(*, store, adapter, guard, asin, profile, sku, bid, policy):
    with store.locked() as state:
        record = state.get("intents", {}).get(asin)
        if state.get("unresolved") != asin or not record:
            raise LaunchBlocked("No reserved launch")
        cid = record.get("campaign_id")
        if not cid:
            raise LaunchBlocked("Campaign creation must be reconciled")
        if record.get("status") in ("manual_reconciliation_required", "uncertain_review_required", "activation_uncertain"):
            raise LaunchBlocked("Uncertain state requires reconciliation")
        if record.get("status") not in ("paused_verified", "provisioned_verified"):
            raise LaunchBlocked("Unrecognized stage; fail closed")
        # Profit evaluation always re-reads stored economics/pacing before writes.
        guard.authorize_paused_launch({"asin": asin, "sku": sku, "campaign_id": cid}, policy, profile)
        if record["status"] == "paused_verified":
            # This step must run under one locked state scope. Provisioner owns
            # its own lock, so it cannot be safely invoked under this lock.
            return {"status": "ready_for_provisioning", "cid": cid, "asin": asin}
        if adapter.verify_provisioning(cid=cid, asin=asin, profile=profile) is not True:
            raise LaunchBlocked("Provisioned entities cannot be verified")
        # Write-ahead mark makes an ambiguous activation non-replayable.
        record["status"] = "activation_uncertain"
        store.save(state)
        try:
            result = adapter.activate_verified(cid=cid, asin=asin, profile=profile)
        except Exception:
            record["status"] = "activation_uncertain"
            store.save(state)
            raise
        if result.get("status") != "enabled_verified":
            record["status"] = "activation_uncertain"
            store.save(state)
            raise LaunchBlocked("Activation not confirmed")
        record["status"] = "enabled_verified"
        state["unresolved"] = None
        store.save(state)
        return result

def reconcile_readonly(*, store, adapter, asin, profile):
    """Return observed Amazon entities without clearing an unknown reservation."""
    with store.locked() as state:
        rec = state.get("intents", {}).get(asin)
        if not rec:
            raise LaunchBlocked("No intent")
        cid = rec.get("campaign_id")
        observations = {"record_status": rec.get("status"),
                        "campaign_id": cid, "requires_review": True}
        if cid:
            observations["paused"] = adapter.confirm_paused_campaign(cid, asin, profile)
            try:
                observations["provisioned"] = adapter.verify_provisioning(
                    cid=cid, asin=asin, profile=profile)
            except Exception:
                observations["provisioned"] = False
        return observations
