"""Stage coordinator for verified automatic launches.

The caller supplies the guarded coordinator, provisioner and activation flow.
No scheduler enables this module by default.
"""
from auto_launch_coordinator import LaunchBlocked
from auto_launch_flow import continue_launch


def run_pipeline(*, coordinator, provisioner, store, adapter, guard,
                 products, verified, policy, account_profile):
    if not coordinator.enable:
        return {'status': 'disabled', 'created': 0}
    start = coordinator.run_one(products=products, verified=verified,
                                policy=policy, account_profile=account_profile)
    if start.get('status') != 'paused_requires_activation':
        return start
    asin, cid = start['asin'], start['campaign_id']
    matches = [v for v in verified.values() if v.get('asin') == asin and v.get('sku')]
    if len(matches) != 1:
        raise LaunchBlocked('Unique verified SKU required')
    sku = matches[0]['sku']
    decision = guard.authorize_paused_launch({'asin': asin, 'sku': sku}, policy, account_profile)
    bid = decision.get('max_bid') if isinstance(decision, dict) else None
    if not isinstance(bid, (int, float)) or isinstance(bid, bool) or not 0.02 <= bid <= 2.50:
        raise LaunchBlocked('Verified CPC required')
    outcome = provisioner.provision(cid=cid, asin=asin, sku=sku,
                                    profile=account_profile, bid=bid)
    if outcome.get('status') != 'provisioned_verified':
        raise LaunchBlocked('Provisioning incomplete')
    return continue_launch(store=store, adapter=adapter, guard=guard,
                           asin=asin, profile=account_profile, sku=sku,
                           bid=bid, policy=policy)
