"""Explicit Amazon Sponsored Products adapter for the opt-in launch coordinator.

All writes go through optimize_campaigns.AmazonAdsClient.post/put, which call
profit_guard.guarded_write. The adapter never calls _request_raw.
"""
from amazon_results import batch_outcome
from auto_launch_coordinator import LaunchBlocked

class ProductionLaunchAmazon:
    def __init__(self, client):
        self.client = client

    def _profile(self, expected):
        if str(self.client.profile_id) != str(expected):
            raise LaunchBlocked("Ads profile mismatch")

    def list_advertised_asins(self, profile):
        self._profile(profile)
        campaigns = self.client.list_campaigns()
        if not isinstance(campaigns, list):
            raise LaunchBlocked("Incomplete Amazon campaign discovery")
        found = set()
        for campaign in campaigns:
            if str(campaign.get("state", "")).upper() == "ARCHIVED":
                continue
            cid = campaign.get("campaignId")
            if not cid:
                raise LaunchBlocked("Missing campaign ID in discovery")
            ads = self.client.list_product_ads(str(cid))
            if not isinstance(ads, list):
                raise LaunchBlocked("Incomplete product ad discovery")
            for ad in ads:
                asin = str(ad.get("asin") or "").strip()
                if not asin and str(ad.get("state", "")).upper() != "ARCHIVED":
                    raise LaunchBlocked("Product ad lacks ASIN")
                if asin:
                    found.add(asin)
        return found

    def create_paused_campaign(self, candidate, profile):
        self._profile(profile)
        # Only create the campaign shell. A distinct, guarded provisioning step
        # must add ads/targets before any activation is attempted.
        asin = candidate["asin"]
        response = self.client.post("/sp/campaigns", {"campaigns":[{
            "name": "NWS AUTO PENDING | " + asin,
            "targetingType": "AUTO",
            "state": "PAUSED",
            "dynamicBidding": {"strategy": "LEGACY_FOR_SALES"},
            "budget": {"budgetType":"DAILY", "budget": 3.0},
        }]})
        outcome = batch_outcome(response, "campaigns", 1)
        if not outcome["success"]:
            raise LaunchBlocked("Amazon did not acknowledge paused campaign")
        items = response.get("campaigns", {}).get("success", [])
        item = items[0]
        created = item.get("campaign") or item
        cid = created.get("campaignId")
        if not cid:
            raise LaunchBlocked("Amazon omitted created campaign ID")
        return {"campaign_id": str(cid)}

    def confirm_paused_campaign(self, cid, asin, profile):
        self._profile(profile)
        matches = [c for c in self.client.list_campaigns()
                   if str(c.get("campaignId")) == str(cid)]
        return len(matches) == 1 and matches[0].get("state") == "PAUSED" and matches[0].get("name") == "NWS AUTO PENDING | " + asin

    def list_campaign_ids_for_asin(self, asin, profile):
        self._profile(profile)
        ids = []
        for campaign in self.client.list_campaigns():
            cid = campaign.get("campaignId")
            if not cid:
                raise LaunchBlocked("Unverifiable campaign ID")
            if any(ad.get("asin") == asin for ad in self.client.list_product_ads(str(cid))):
                ids.append(str(cid))
        return ids

    def activate_verified(self, *, cid, asin, profile, provisioned):
        """Only enable after all objects and economics are independently verified."""
        self._profile(profile)
        if provisioned is not True:
            raise LaunchBlocked("Ad group, product ad and targeting not verified")
        campaigns = [c for c in self.client.list_campaigns() if str(c.get("campaignId")) == str(cid)]
        if len(campaigns) != 1 or campaigns[0].get("state") != "PAUSED":
            raise LaunchBlocked("Campaign not uniquely paused")
        ads = self.client.list_product_ads(str(cid))
        if not any(str(a.get("asin")) == asin for a in ads):
            raise LaunchBlocked("Advertised ASIN not verified")
        # Guarded client PUT enforces policy and fresh account limits.
        response = self.client.put("/sp/campaigns", {"campaigns":[{"campaignId":str(cid),"state":"ENABLED"}]})
        if not batch_outcome(response, "campaigns", 1)["success"]:
            raise LaunchBlocked("Activation not acknowledged")
        updated = [c for c in self.client.list_campaigns() if str(c.get("campaignId")) == str(cid)]
        if len(updated) != 1 or updated[0].get("state") != "ENABLED":
            raise LaunchBlocked("Activation state not confirmed")
        return {"campaign_id":str(cid),"status":"enabled_verified"}
