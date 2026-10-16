"""Approved live validation: no deployment, policy updates or advertising writes.

Credentials remain in runner memory. Only allowlisted check results are emitted.
Storage probes use a unique disposable namespace, never production workflow keys.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ValidationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def gcloud(*args):
    result = subprocess.run(['gcloud', *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        denied = 'PERMISSION_DENIED' in result.stderr or 'Permission' in result.stderr
        operation = 'runtime_impersonation' if args[:2] == ('auth','print-access-token') else 'cloud_read'
        code = operation + ('_denied' if denied else '_failed')
        if 'SERVICE_DISABLED' in result.stderr or 'has not been used in project' in result.stderr:
            code = 'iam_credentials_api_disabled'
        elif 'serviceusage.services.use' in result.stderr:
            code = 'service_usage_permission_denied'
        elif 'ACCESS_TOKEN_SCOPE_INSUFFICIENT' in result.stderr:
            code = 'credential_scope_insufficient'
        raise ValidationError(code)
    return result.stdout.strip()


def load_runtime():
    project = 'amazon-ppc-bid-optimizer'
    config = json.loads(gcloud('run','services','describe','campaign-optimizer',
        '--project',project,'--region','us-central1','--format=json'))
    wanted = {'AMAZON_ADS_CLIENT_ID','AMAZON_ADS_CLIENT_SECRET','AMAZON_ADS_REFRESH_TOKEN',
        'AMAZON_ADS_PROFILE_ID','AMAZON_ADS_REGION','AMAZON_ADS_TIMEZONE',
        'AUTOMATION_STATE_BUCKET','SP_API_CLIENT_ID','SP_API_CLIENT_SECRET',
        'SP_API_REFRESH_TOKEN','SP_API_SELLER_ID','SP_API_MARKETPLACE_ID'}
    for item in config['spec']['template']['spec']['containers'][0].get('env',[]):
        name = item['name']
        if name not in wanted: continue
        if 'value' in item:
            os.environ[name] = item['value']
        else:
            ref = item['valueFrom']['secretKeyRef']
            os.environ[name] = gcloud('secrets','versions','access',ref['key'],
                '--secret='+ref['name'],'--project',project)
    os.environ['GCP_PROJECT_ID'] = project
    # Do not silently choose another bucket or account.
    for key in ('AMAZON_ADS_PROFILE_ID','AUTOMATION_STATE_BUCKET','AMAZON_ADS_TIMEZONE'):
        if not os.getenv(key): raise RuntimeError('Missing runtime validation context')
    return config


def bind_storage_token():
    """Use the approved runtime identity for GCS, including the child process."""
    token = os.getenv('VALIDATION_STORAGE_TOKEN')
    if not token: raise RuntimeError('Runtime storage identity unavailable')
    from google.cloud import storage
    from google.oauth2.credentials import Credentials
    original = storage.Client
    def runtime_client(*args, **kwargs):
        kwargs['credentials'] = Credentials(token=token)
        kwargs.setdefault('project', 'amazon-ppc-bid-optimizer')
        return original(*args, **kwargs)
    storage.Client = runtime_client
    return original


def storage_child(name, nonce):
    from automation_store import GCSState, StateBusy
    if not name.startswith('validation-live/') or '/' in nonce:
        return 2
    bind_storage_token()
    probe = GCSState(name)
    if probe.read() != {'nonce':nonce}: return 3
    try:
        with probe.locked(): return 4  # The parent must still own this lock.
    except StateBusy:
        return 0


def storage_probe():
    from automation_store import GCSState
    name, nonce = 'validation-live/' + uuid4().hex, uuid4().hex
    probe = GCSState(name)
    generation = None
    try:
        with probe.locked():
            probe.save({'nonce':nonce})
            probe.blob.reload()
            generation = probe.blob.generation
            child = subprocess.run([sys.executable,__file__,'--storage-child',name,nonce],
                capture_output=True, text=True, timeout=45)
            if child.returncode: raise RuntimeError('Cross-process storage verification failed')
        # A new instance must reacquire the released lock and see the same snapshot.
        with GCSState(name).locked() as value:
            if value != {'nonce':nonce}: raise RuntimeError('Storage readback mismatch')
    finally:
        if generation is not None:
            probe.blob.delete(if_generation_match=generation)


def main():
    results = []
    def check(name, fn):
        try:
            details = fn() or {}
            results.append({'check':name,'status':'passed',**details})
        except Exception as exc:
            # No raw exception text, response bodies, headers, environment or IDs.
            results.append({'check':name,'status':'failed','error_type':type(exc).__name__, 'reason_code':getattr(exc,'code','operation_failed')})
    config = load_runtime()
    check('runtime_revision', lambda: {'revision':config['status'].get('latestReadyRevisionName'),
        'auto_launch_enabled':any(e['name']=='AUTO_LAUNCH_ENABLED' and e.get('value','').lower()=='true'
            for e in config['spec']['template']['spec']['containers'][0].get('env',[]))})
    from optimize_campaigns import AmazonAdsClient
    from decision_sync import SellingPartnerClient, refresh_product_market
    from automation_store import GCSState
    from profit_intelligence import Policy, economics, inventory_factor
    from profit_guard import bid_preflight

    class ReadOnlyAds(AmazonAdsClient):
        def request(self, method, endpoint, *args, **kwargs):
            allowed = {'/sp/campaigns/list','/sp/adGroups/list','/sp/productAds/list',
                       '/sp/keywords/list','/sp/targets/list'}
            if not (method.upper()=='GET' and endpoint=='/v2/profiles' or
                    method.upper()=='POST' and endpoint in allowed):
                raise RuntimeError('Advertising mutation blocked by validation adapter')
            return super().request(method, endpoint, *args, **kwargs)

    class ReadOnlyRetail(SellingPartnerClient):
        def request(self, method, path, params=None, body=None):
            allowed = method=='GET' and (path.startswith('/listings/') or path.startswith('/fba/inventory/'))
            estimate = method=='POST' and path.startswith('/products/fees/') and path.endswith('/feesEstimate')
            if not (allowed or estimate): raise RuntimeError('Retail write blocked by validation adapter')
            return super().request(method,path,params,body)

    ads = None
    def ads_auth():
        nonlocal ads
        ads = ReadOnlyAds()
        profiles = ads.get('/v2/profiles')
        match = [p for p in profiles if str(p.get('profileId'))==str(ads.profile_id)]
        if len(match)!=1 or match[0].get('currencyCode')!='USD' or match[0].get('timezone')!=os.environ['AMAZON_ADS_TIMEZONE']:
            raise RuntimeError('Profile currency or timezone mismatch')
        return {'currency':'USD','timezone':match[0]['timezone']}
    check('amazon_ads_authorization', ads_auth)
    data, policy = {}, None
    def saved_inputs():
        nonlocal data, policy
        data = GCSState('profit-data').read()
        policy = Policy.model_validate(GCSState('profit-policy').read()['policy'])
        return {'products':len(data.get('products',{})), 'metrics':len(data.get('metrics',[])),
                'daily_limit':policy.daily_limit,'complete_spend_age_minutes':
                    round((time.time()-data['complete_spend_at'])/60,1) if data.get('complete_spend_at') else None}
    check('saved_policy_and_data', saved_inputs)
    def runtime_storage():
        identity = config['spec']['template']['spec'].get('serviceAccountName')
        if not identity:
            identity = gcloud('run','revisions','describe',config['status']['latestReadyRevisionName'],
                '--project','amazon-ppc-bid-optimizer','--region','us-central1',
                '--format=value(spec.serviceAccountName)')
        if not identity: raise ValidationError('runtime_identity_not_explicit')
        # Token issuance only: do not grant roles or modify service accounts.
        os.environ['VALIDATION_STORAGE_TOKEN'] = gcloud('auth','print-access-token',
            '--impersonate-service-account='+identity)
        original = bind_storage_token()
        try:
            storage_probe()
            return {'identity_scope':'cloud_run_runtime_service_account'}
        finally:
            from google.cloud import storage
            storage.Client = original
            os.environ.pop('VALIDATION_STORAGE_TOKEN',None)
    check('gcs_runtime_cross_process_persistence_and_lock',runtime_storage)
    if ads is not None:
        def objects_and_caps():
            campaigns = ads.list_campaigns()
            groups = ads.list_all('/sp/adGroups/list','adGroups')
            counts = {'campaigns':len(campaigns),'groups':len(groups),'empty_campaigns':0,
                      'eligible_bid_samples':0,'held_bid_samples':0,'unverifiable_bid_samples':0}
            # Bound live API load; do not interpret a sample as account-wide verification.
            sampled = campaigns[:3]
            for campaign in sampled:
                cid = str(campaign['campaignId'])
                product_ads = ads.list_product_ads(cid)
                if not any(a.get('state')!='ARCHIVED' for a in product_ads): counts['empty_campaigns']+=1
                ads.list_keywords(cid)
                ads.list_targets(cid)
                group = next((g for g in groups if str(g.get('campaignId'))==cid),None)
                if group and policy:
                    try:
                        result=bid_preflight(ads,'/sp/adGroups',{'adGroupId':str(group['adGroupId'])},
                                             data=data,policy=policy)
                        counts['eligible_bid_samples' if result['status']=='eligible' else 'held_bid_samples']+=1
                    except Exception:
                        counts['unverifiable_bid_samples']+=1
            return {**counts,'sampled_campaigns':len(sampled)}
        check('amazon_list_shapes_and_bid_preflight',objects_and_caps)
    def product_market():
        if not data.get('products') or not policy: raise RuntimeError('No saved verified product')
        product=next(iter(data['products'].values()))
        refreshed=refresh_product_market(ReadOnlyRetail(),product)
        economics(refreshed,policy,time.time())
        inventory_factor(refreshed,policy,time.time())
        return {'sampled_products':1,'listing_identity_matched':True,'fees_and_inventory_refreshed_in_memory':True}
    check('selling_partner_authorization_and_product_lookup',product_market)
    # Direct preview of new code against real observations; never call live endpoints.
    def paused_launch_readiness():
        if ads is None or not policy: raise RuntimeError('Required observations unavailable')
        from auto_launch_guard import ProductionLaunchGuard
        from auto_launch_coordinator import LaunchBlocked
        from profit_guard import GuardBlocked
        approved = [p for a,p in data.get('products',{}).items() if a in policy.product_exploration]
        if not approved: return {'readiness':'held','reason':'no_product_exploration_allowance'}
        try:
            decision=ProductionLaunchGuard(ads).authorize_paused_launch(approved[0],policy.model_dump(),ads.profile_id)
            return {'readiness':'eligible','max_bid':decision['max_bid'],'budget_cap':decision['budget_cap']}
        except (LaunchBlocked,GuardBlocked,ValueError):
            return {'readiness':'held','reason':'existing_safety_gate'}
    check('paused_launch_readiness_without_creation',paused_launch_readiness)
    result={'success':all(r['status']=='passed' for r in results),'checks':results,
            'advertising_writes':0,'deployment_changes':0,'production_state_updates':0}
    print('CONTROLLED_LIVE_VALIDATION '+json.dumps(result))
    summary=os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a') as f:f.write('## Controlled live validation\n```json\n'+json.dumps(result,indent=2)+'\n```\n')
    return 0 if result['success'] else 1

if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--storage-child':
        try:sys.exit(storage_child(sys.argv[2],sys.argv[3]))
        except Exception:sys.exit(5)
    try:sys.exit(main())
    except Exception as exc:
        print('CONTROLLED_LIVE_VALIDATION_SETUP_FAILED '+type(exc).__name__)
        sys.exit(1)
