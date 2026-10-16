"""Opt-in automatic launch selection. No Amazon mutations happen here.

The existing guarded /api/create-campaign-from-product endpoint remains the
only launch executor. A scheduler should call plan_auto_launch and, only after
explicit operator activation, invoke that endpoint with apply_live=True.
"""
from __future__ import annotations
import math
from tenant_context import TenantConfigurationError

def eligible_auto_launch(products, verified, existing_asins, policy, *, max_new=1):
    """Return approved candidates, never invent product economics."""
    if not 0 <= max_new <= 10:
        raise ValueError("Invalid maximum campaigns")
    if not isinstance(verified, dict) or not policy:
        raise ValueError("Verified product economics and policy required")
    result = []
    for product in products:
        asin = str(product.get("asin") or product.get("ASIN") or "").strip()
        if not asin or asin in existing_asins or asin not in verified:
            continue
        v = verified[asin]
        if str(v.get("asin")) != asin or not v.get("sku"):
            continue
        required = ("price", "cogs", "shipping", "fees", "available")
        if any(k not in v for k in required):
            continue
        try:
            nums = [float(v[k]) for k in required]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(n) and n >= 0 for n in nums):
            continue
        price, cogs, shipping, fees, available = nums
        if price <= 0 or available < 5:
            continue
        reserve = float(policy.get("profit_reserve", .10))
        if not math.isfinite(reserve) or not 0 <= reserve < 1:
            raise ValueError("Invalid reserve")
        if price - cogs - shipping - fees <= price * reserve:
            continue
        result.append({"asin": asin, "sku": v["sku"], "product_id": str(product.get("product_id") or product.get("Product_ID") or asin)})
        if len(result) == max_new:
            break
    return result if max_new else []
