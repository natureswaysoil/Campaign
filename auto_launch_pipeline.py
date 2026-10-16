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
    start = None
    if store is not None:
        # Stages each acquire this same profile lock. Never nest it.
        with store.locked() as state:
            asin = state.get('unresolved')
            rec = state.get('intents', {}).get(asin) if asin else None
            if rec:
                if rec.get('profile') != str(account_profile):
                    raise LaunchBlocked('Intent profile mismatch')
                if hasattr(adapter, 'launch_names'):
                    adapter.launch_names[asin] = rec.get('launch_name')
                if not rec.get('campaign_id'):
                    # Exact durable launch name, never a title or ASIN-only fuzzy match.
                    matches = [c for c in adapter.client.list_campaigns()
                               if rec.get('launch_name') and c.get('name') == rec['launch_name']]
                    if len(matches) != 1 or matches[0].get('state') != 'PAUSED':
                        raise LaunchBlocked('Campaign creation outcome still uncertain; no POST replay')
                    rec.update(campaign_id=str(matches[0]['campaignId']), status='paused_verified')
                    store.save(state)
                if rec.get('status') == 'activation_uncertain':
                    if adapter.confirm_enabled_launch(cid=rec['campaign_id'], asin=asin, sku=rec['sku'], profile=account_profile):
                        rec['status'] = 'enabled_verified'
                        state['unresolved'] = None
                        store.save(state)
                        return {'status': 'enabled_verified', 'campaign_id': rec['campaign_id'], 'reconciled': True}
                    raise LaunchBlocked('Activation outcome uncertain; no activation replay')
                start = {'status': 'paused_requires_activation', 'asin': asin, 'campaign_id': rec['campaign_id']}
    if start is None:
        start = coordinator.run_one(products=products, verified=verified,
                                    policy=policy, account_profile=account_profile)
    if start.get('status') != 'paused_requires_activation':
        return start
    asin, cid = start['asin'], start['campaign_id']
    matches = [v for v in verified.values() if v.get('asin') == asin and v.get('sku')]
    if len(matches) != 1:
        raise LaunchBlocked('Unique verified SKU required')
    sku = matches[0]['sku']
    if hasattr(adapter, 'expected_skus'):
        adapter.expected_skus[asin] = sku
    decision = guard.authorize_paused_launch({'asin': asin, 'sku': sku, 'campaign_id': cid}, policy, account_profile)
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
