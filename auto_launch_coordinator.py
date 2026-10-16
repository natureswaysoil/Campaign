"""Fail-closed launch orchestrator, intentionally not routed or scheduled.

A verified account adapter must supply Amazon discovery and create callbacks.
A failed/ambiguous operation stays in a durable pending state for manual
reconciliation. This never creates an ENABLED campaign directly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from auto_launch_policy import eligible_auto_launch

class LaunchBlocked(RuntimeError):
    pass

def _utc():
    return datetime.now(timezone.utc).isoformat()

class AutoLaunchCoordinator:
    def __init__(self, *, store, amazon, guard, enable=False):
        self.store, self.amazon, self.guard = store, amazon, guard
        self.enable = enable

    def run_one(self, *, products, verified, policy, account_profile):
        if not self.enable:
            return {"status": "disabled", "created": 0}
        if not policy or not policy.get("daily_limit"):
            raise LaunchBlocked("Account profit policy required")
        # One shared lock must serialize all launches for this Ads profile.
        with self.store.locked() as state:
            state.setdefault("intents", {})
            if state.get("unresolved"):
                raise LaunchBlocked("Previous uncertain launch requires reconciliation")
            # Read actual Amazon product ads; missing/stale data must be fatal.
            existing = self.amazon.list_advertised_asins(account_profile)
            if not isinstance(existing, set):
                raise LaunchBlocked("Full existing ASIN set required")
            pending = set(state["intents"])
            choices = eligible_auto_launch(
                products, verified, existing | pending, policy, max_new=1)
            if not choices:
                return {"status": "no_eligible_products", "created": 0}
            candidate = choices[0]
            asin = candidate["asin"]
            # Expected preflight holds must not leave an uncertain write intent.
            authorization = self.guard.authorize_paused_launch(candidate, policy, account_profile)
            if not isinstance(authorization, dict) or "budget_cap" not in authorization or "max_bid" not in authorization:
                raise LaunchBlocked("Guard did not return verified budget and CPC cap")
            launch_id = uuid4().hex
            launch_name = "NWS AUTO | " + asin + " | " + launch_id
            state["intents"][asin] = {"status": "reserved", "at": _utc(),
                "launch_id": launch_id, "launch_name": launch_name,
                "profile": str(account_profile), "sku": candidate['sku']}
            state["unresolved"] = asin
            self.store.save(state)
            candidate = {**candidate, 'launch_name': launch_name}
            candidate = {**candidate, **authorization}
            cid = ""
            try:
                response = self.amazon.create_paused_campaign(candidate, account_profile)
                # Require explicit creation ID and independently read it back.
                cid = str(response.get("campaign_id") or "")
                if not cid or not self.amazon.confirm_paused_campaign(cid, asin, account_profile):
                    raise LaunchBlocked("Paused creation unconfirmed")
                state["intents"][asin].update(status="paused_verified", campaign_id=cid, at=_utc())
                self.store.save(state)
                # Deliberately requires separate guarded authorization; this
                # component does not activate campaigns in the same invocation.
                state["unresolved"] = asin
                self.store.save(state)
                return {"status": "paused_requires_activation", "campaign_id": cid, "asin": asin, "created": 1}
            except Exception:
                state["intents"][asin].update(status="uncertain_review_required", at=_utc(),
                    **({"campaign_id": cid} if cid else {}))
                state["unresolved"] = asin
                self.store.save(state)
                raise

    def reconcile(self, *, asin, account_profile):
        """Read-only acknowledgement of an existing paused creation.

        No automatic clearing: an operator must verify the ledger and Amazon
        state before explicitly authorizing resumption.
        """
        with self.store.locked() as state:
            intent = state.get("intents", {}).get(asin)
            if not intent:
                raise LaunchBlocked("No launch intent")
            ids = self.amazon.list_campaign_ids_for_asin(asin, account_profile)
            if not isinstance(ids, list):
                raise LaunchBlocked("Amazon verification unavailable")
            return {"asin": asin, "status": intent["status"], "amazon_ids": ids,
                    "manual_review_required": True}
