"""Guarded paused-campaign provisioning with durable write-ahead checkpoints.

Does not enable campaigns. Every uncertain response preserves a pending marker:
operators must reconcile Amazon's actual entities before retrying.
"""
from auto_launch_coordinator import LaunchBlocked
from amazon_results import batch_outcome

class PausedProvisioner:
    def __init__(self, *, store, adapter):
        self.store, self.adapter = store, adapter

    def provision(self, *, cid, asin, sku, profile, bid, keyword=None):
        if not cid or not asin or not sku or not profile or not (0.02 <= bid <= 2.50):
            raise LaunchBlocked("Invalid provisioning input")
        with self.store.locked() as state:
            intent = state.get("intents", {}).get(asin)
            if not intent or intent.get("campaign_id") != str(cid) or state.get("unresolved") != asin:
                raise LaunchBlocked("No matching durable paused intent")
            if intent.get("status") != "paused_verified":
                raise LaunchBlocked("Manual reconciliation required")
            # Persist the uncertain flag before each Amazon write.
            def write(stage, endpoint, key, payload):
                intent["status"] = "uncertain_" + stage
                self.store.save(state)
                response = self.adapter.client.post(endpoint, {key: [payload]})
                if not batch_outcome(response, key, 1)["success"]:
                    raise LaunchBlocked("Amazon response unconfirmed at " + stage)
                return response
            self.adapter._profile(profile)
            if not self.adapter.confirm_paused_campaign(cid, asin, profile):
                raise LaunchBlocked("Amazon campaign not verified paused")
            try:
                group_response = write("ad_group", "/sp/adGroups", "adGroups", {
                    "campaignId": str(cid), "name": "NWS AUTO | " + asin,
                    "state": "ENABLED", "defaultBid": bid,
                })
                data = group_response["adGroups"]["success"][0]
                group_id = str((data.get("adGroup") or data).get("adGroupId") or "")
                if not group_id:
                    raise LaunchBlocked("Ad group ID not confirmed")
                intent.update(status="ad_group_ack", ad_group_id=group_id)
                self.store.save(state)
                write("product_ad", "/sp/productAds", "productAds", {
                    "campaignId": str(cid), "adGroupId": group_id,
                    "state": "ENABLED", "sku": sku, "asin": asin,
                })
                intent["status"] = "product_ad_ack"
                self.store.save(state)
                if keyword:
                    raise LaunchBlocked("Manual keyword provisioning not implemented")
                write("auto_target", "/sp/targets", "targetingClauses", {
                    "campaignId": str(cid), "adGroupId": group_id,
                    "state": "ENABLED", "expressionType": "AUTO",
                    "expression": [{"type": "CLOSE_MATCH"}], "bid": bid,
                })
                intent["status"] = "auto_target_ack"
                self.store.save(state)
                if self.adapter.verify_provisioning(cid=cid, asin=asin, profile=profile) is not True:
                    raise LaunchBlocked("Amazon entity readback did not confirm provisioning")
                intent["status"] = "provisioned_verified"
                self.store.save(state)
                return {"status": "provisioned_verified", "campaign_id": str(cid)}
            except Exception:
                # Never retry non-idempotent Amazon writes on unknown outcomes.
                if intent["status"] != "provisioned_verified":
                    intent["status"] = "manual_reconciliation_required"
                    self.store.save(state)
                raise
