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

    def verify_provisioning(self, *, cid, asin, profile):
        """Independently read back every required entity; fail closed."""
        self._profile(profile)
        campaigns = [x for x in self.client.list_campaigns()
                     if str(x.get("campaignId")) == str(cid)]
        if len(campaigns) != 1 or campaigns[0].get("state") != "PAUSED":
            raise LaunchBlocked("Campaign must be uniquely paused")
        groups = self.client.list_all("/sp/adGroups/list", "adGroups",
            {"campaignIdFilter": {"include": [str(cid)]}})
        if not groups or not all(str(g.get("campaignId")) == str(cid) for g in groups):
            raise LaunchBlocked("Ad groups absent or inconsistent")
        active_groups = {str(g["adGroupId"]) for g in groups if g.get("state") == "ENABLED" and g.get("adGroupId")}
        if not active_groups:
            raise LaunchBlocked("No enabled ad group")
        ads = self.client.list_product_ads(str(cid))
        if not any(str(a.get("asin")) == str(asin) and str(a.get("adGroupId")) in active_groups
                   and a.get("state") == "ENABLED" for a in ads):
            raise LaunchBlocked("Enabled product ad not verified")
        if campaigns[0].get("targetingType") == "AUTO":
            targets = self.client.list_targets(str(cid))
            if not any(str(t.get("adGroupId")) in active_groups and t.get("state") == "ENABLED"
                       for t in targets):
                raise LaunchBlocked("Automatic target not verified")
        else:
            keywords = self.client.list_keywords(str(cid))
            if not any(str(k.get("adGroupId")) in active_groups and k.get("state") == "ENABLED"
                       for k in keywords):
                raise LaunchBlocked("Manual targeting not verified")
        return True

    def activate_verified(self, *, cid, asin, profile, provisioned=None):
        """Only enable after all objects and economics are independently verified."""
        self._profile(profile)
        self.verify_provisioning(cid=cid, asin=asin, profile=profile)
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
