"""Production fail-closed preflight for guarded automatic Sponsored Products launches."""
import time
from auto_launch_coordinator import LaunchBlocked
from profit_intelligence import Policy, Product, economics, inventory_factor, pacing, cpc_ceiling
from automation_store import GCSState

class ProductionLaunchGuard:
    def __init__(self, client):
        self.client = client

    def authorize_paused_launch(self, candidate, supplied_policy, account_profile):
        if str(self.client.profile_id) != str(account_profile):
            raise LaunchBlocked("Profile mismatch")
        stored = GCSState("profit-policy").read().get("policy")
        if not stored:
            raise LaunchBlocked("Profit policy missing")
        policy = Policy.model_validate(stored)
        if not supplied_policy or float(supplied_policy.get("daily_limit", 0)) != policy.daily_limit:
            raise LaunchBlocked("Requested and stored profit limits differ")
        state = GCSState("profit-data").read()
        raw = state.get("products", {}).get(candidate["asin"])
        if not raw:
            raise LaunchBlocked("Verified ASIN economics missing")
        product = Product.model_validate(raw)
        if product.sku != candidate["sku"]:
            raise LaunchBlocked("ASIN SKU mismatch")
        # Economics validates current cost, fees, stock and demand freshness.
        details = economics(product.model_dump(mode="json"), policy, time.time())
        stock_factor, _ = inventory_factor(product.model_dump(mode="json"), policy, time.time())
        if stock_factor <= 0:
            raise LaunchBlocked("No usable inventory cover")
        if not details or details["target_acos"] <= 0:
            raise LaunchBlocked("No allowable advertising margin")
        pulse = pacing(state, policy, time.time())
        if not pulse["fresh"] or pulse["stop"]:
            raise LaunchBlocked("Account spend not current or stop threshold reached")
        if not policy.exploration_cvr and candidate["asin"] not in policy.product_exploration:
            raise LaunchBlocked("No approved exploration allowance")
        opt = policy.product_exploration.get(candidate["asin"])
        budget = opt.daily_budget if opt else policy.exploration_daily_budget
        budget = min(budget, policy.daily_limit - pulse["spend"])
        if budget < 1.0:
            raise LaunchBlocked("Insufficient remaining daily budget")
        cap = cpc_ceiling(product.model_dump(mode="json"), [], policy, time.time())["max_cpc"]
        if cap < policy.min_cpc:
            raise LaunchBlocked("Unprofitable CPC ceiling")
        from profit_guard import Guard
        # Check the aggregate enabled/reserved envelope before even a paused shell.
        self.client.profit_product_asin = candidate['asin']
        ledger = GCSState('profit-write-guard').read()
        Guard(self.client, state, policy, ledger, time.time()).account_room(
            str(candidate.get('campaign_id') or 'new:auto-launch'), budget)
        return {"budget_cap": round(budget, 2), "max_bid": cap}
