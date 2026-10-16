"""Checkpoint each create and reconcile acknowledged or uncertain stages by IDs.

An absent entity after an uncertain POST is never evidence that a retry is safe.
"""
from auto_launch_coordinator import LaunchBlocked
from amazon_results import batch_outcome

class PausedProvisioner:
    def __init__(self, *, store, adapter):
        self.store, self.adapter = store, adapter

    def provision(self, *, cid, asin, sku, profile, bid, keyword=None):
        if not cid or not asin or not sku or not profile or not (0.02 <= bid <= 2.50) or keyword:
            raise LaunchBlocked('Invalid or unsupported provisioning input')
        with self.store.locked() as state:
            intent = state.get('intents', {}).get(asin)
            if not intent or intent.get('campaign_id') != str(cid) or state.get('unresolved') != asin:
                raise LaunchBlocked('No matching durable paused intent')
            if intent.get('profile', str(profile)) != str(profile) or intent.get('sku', sku) != sku:
                raise LaunchBlocked('Intent identity mismatch')
            allowed = {'paused_verified', 'provisioned_verified', 'ad_group_ack', 'product_ad_ack',
                       'auto_target_ack', 'manual_reconciliation_required'}
            if intent.get('status') not in allowed and not str(intent.get('status', '')).startswith('uncertain_'):
                raise LaunchBlocked('Manual reconciliation required')
            self.adapter._profile(profile)
            self.adapter.client.profit_product_asin = asin
            if not self.adapter.confirm_paused_campaign(cid, asin, profile):
                raise LaunchBlocked('Amazon campaign not verified paused')
            group_name = 'NWS AUTO | ' + asin
            client = self.adapter.client
            def stage(name, endpoint, key, id_key, payload, observe):
                checkpoints = intent.setdefault('entities', {})
                # Readbacks are mandatory even when the acknowledgement was saved.
                if name in checkpoints or intent.get('pending_stage') == name:
                    try:
                        matches = observe()
                    except Exception as exc:
                        raise LaunchBlocked('Reconciliation unavailable at ' + name) from exc
                    expected = checkpoints.get(name)
                    if len(matches) != 1 or not matches[0].get(id_key) or (expected and str(matches[0][id_key]) != expected):
                        raise LaunchBlocked('Uncertain or ambiguous entity at ' + name)
                    entity_id = str(matches[0][id_key])
                else:
                    # Detect preexisting objects before a POST, including default AUTO targets.
                    matches = observe()
                    if len(matches) > 1:
                        raise LaunchBlocked('Ambiguous existing entity at ' + name)
                    if matches:
                        entity_id = str(matches[0].get(id_key) or '')
                        if not entity_id:
                            raise LaunchBlocked('Missing canonical ID at ' + name)
                    else:
                        intent.update(status='uncertain_' + name, pending_stage=name)
                        self.store.save(state)
                        response = client.post(endpoint, {key: [payload]})
                        if not batch_outcome(response, key, 1)['success']:
                            raise LaunchBlocked('Amazon response unconfirmed at ' + name)
                        # Never infer creation from a submitted count or missing ID.
                        success = response[key]['success'][0]
                        nested = {'adGroups': 'adGroup', 'productAds': 'productAd', 'targetingClauses': 'targetingClause'}[key]
                        entity_id = str((success.get(nested) or success).get(id_key) or '')
                        if not entity_id:
                            raise LaunchBlocked('Missing acknowledged ID at ' + name)
                        checkpoints[name] = entity_id
                        self.store.save(state)
                        matches = observe()
                        if len(matches) != 1 or str(matches[0].get(id_key)) != entity_id:
                            raise LaunchBlocked('Readback unconfirmed at ' + name)
                checkpoints[name] = entity_id
                intent['status'] = name + '_ack'
                if intent.get('pending_stage') == name:
                    intent['pending_stage'] = None
                self.store.save(state)
                return entity_id
            try:
                def groups():
                    rows = client.list_all('/sp/adGroups/list', 'adGroups', {'campaignIdFilter': {'include': [str(cid)]}})
                    active = [g for g in rows if g.get('state') != 'ARCHIVED']
                    if any(str(g.get('campaignId')) != str(cid) or g.get('name') != group_name
                           or g.get('state') != 'ENABLED' or float(g.get('defaultBid', 0)) > bid for g in active):
                        raise LaunchBlocked('Unexpected ad group in reserved campaign')
                    return active
                gid = stage('ad_group', '/sp/adGroups', 'adGroups', 'adGroupId',
                    {'campaignId': str(cid), 'name': group_name, 'state': 'ENABLED', 'defaultBid': bid}, groups)
                intent['ad_group_id'] = gid
                def ads():
                    rows = [a for a in client.list_product_ads(str(cid)) if a.get('state') != 'ARCHIVED']
                    if any(a.get('asin') != asin or a.get('sku') != sku or str(a.get('adGroupId')) != gid
                           or a.get('state') != 'ENABLED' for a in rows):
                        raise LaunchBlocked('Product ad identity mismatch')
                    return rows
                stage('product_ad', '/sp/productAds', 'productAds', 'adId',
                    {'campaignId': str(cid), 'adGroupId': gid, 'state': 'ENABLED', 'sku': sku, 'asin': asin}, ads)
                def targets():
                    rows = [t for t in client.list_targets(str(cid)) if t.get('state') != 'ARCHIVED'
                            and t.get('expression') == [{'type': 'CLOSE_MATCH'}]]
                    if any(str(t.get('adGroupId')) != gid or t.get('state') != 'ENABLED'
                           or t.get('expressionType') != 'AUTO' or float(t.get('bid', 0)) > bid for t in rows):
                        raise LaunchBlocked('Automatic targeting identity mismatch')
                    return rows
                stage('auto_target', '/sp/targets', 'targetingClauses', 'targetId',
                    {'campaignId': str(cid), 'adGroupId': gid, 'state': 'ENABLED',
                     'expressionType': 'AUTO', 'expression': [{'type': 'CLOSE_MATCH'}], 'bid': bid}, targets)
                if self.adapter.verify_provisioning(cid=cid, asin=asin, profile=profile) is not True:
                    raise LaunchBlocked('Provisioning not independently verified')
                intent['status'] = 'provisioned_verified'
                self.store.save(state)
                return {'status': 'provisioned_verified', 'campaign_id': str(cid)}
            except Exception:
                intent['status'] = 'manual_reconciliation_required'
                self.store.save(state)
                raise
