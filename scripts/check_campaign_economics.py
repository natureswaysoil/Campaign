"""Inspect exact advertised ASINs and safe readiness for target campaigns.

Read-only; run in an authenticated production environment.
"""
import json
from automation_store import GCSState
from optimize_campaigns import AmazonAdsClient
from scripts.audit_bid_readiness import audit
from profit_intelligence import Policy

CAMPAIGNS = {"276217364626333", "158113174050147", "223500130049181"}


def main():
    data = GCSState("profit-data").read()
    policy_data = GCSState("profit-policy").read().get("policy")
    policy = Policy.model_validate(policy_data) if policy_data else None
    result = audit(AmazonAdsClient(), data.get("products", {}), policy,
                   data.get("metrics", []))
    result["campaigns"] = [c for c in result["campaigns"]
                            if c["campaign_id"] in CAMPAIGNS]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
