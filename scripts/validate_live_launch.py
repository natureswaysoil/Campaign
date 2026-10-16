"""Owner-approved one-campaign lifecycle probe. Future-dated; always pause.

No existing campaign, product economics, policy, scheduler or deployment is edited.
The existing profit guard authorizes every create and activation. Durable intent
and deterministic namespace make reruns reconcile the same probe, not create more.
"""
from __future__ import annotations
import json
import re
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validate_live_readonly import load_runtime, gcloud, bind_storage_token, ValidationError

NAMESPACE='validation-launch/20261016-pr47-auto-1'
ASIN='B0D7T3TLQP'


def run(cleanup_only=False):
    config=load_runtime()
    identity=config['spec']['template']['spec'].get('serviceAccountName')
    if not identity: raise ValidationError('runtime_identity_not_explicit')
    os.environ['VALIDATION_STORAGE_TOKEN']=gcloud('auth','print-access-token','--impersonate-service-account='+identity)
    bind_storage_token()
    from automation_store import GCSState
    from optimize_campaigns import AmazonAdsClient
    from auto_launch_production import ProductionLaunchAmazon
    from auto_launch_guard import ProductionLaunchGuard
    from auto_launch_provisioning import PausedProvisioner
    from auto_launch_flow import continue_launch
    from auto_launch_coordinator import LaunchBlocked
    from amazon_results import batch_outcome
    from profit_intelligence import Policy, ET

    store=GCSState(NAMESPACE)
    result={'success':False,'campaign_created':False,'provisioned':False,'activation_verified':False,
            'final_paused_verified':False,'future_dated':True,'deployment_changes':0,'scheduler_changes':0}
    client=AmazonAdsClient()
    # Bound this diagnostic client's mutations to one exact, durable campaign.
    class ProbeClient:
        def __init__(self,inner):
            self.inner=inner
            self.profile_id=inner.profile_id
            self.profit_product_asin=ASIN
            inner.profit_product_asin=ASIN
            self.last_error={}
        def __getattr__(self,name):return getattr(self.inner,name)
        def post(self,endpoint,body,*args,**kwargs):
            rec=store.read().get('intents',{}).get(ASIN,{})
            if endpoint=='/sp/campaigns':
                rows=body.get('campaigns',[])
                if len(rows)!=1 or rows[0].get('name')!=rec.get('launch_name') or rows[0].get('state')!='PAUSED':
                    raise RuntimeError('Unexpected campaign create')
                if rows[0].get('startDate')!=rec.get('start_date') or float(rows[0]['budget']['budget'])>2:
                    raise RuntimeError('Probe date or budget exceeds authorization')
            else:
                keys={'/sp/adGroups':'adGroups','/sp/productAds':'productAds','/sp/targets':'targetingClauses'}
                if endpoint not in keys:raise RuntimeError('Unexpected probe mutation')
                rows=body.get(keys[endpoint],[])
                if len(rows)!=1 or str(rows[0].get('campaignId'))!=str(rec.get('campaign_id')):
                    raise RuntimeError('Probe campaign mismatch')
            def summarize(value):
                codes, fields = set(), set()
                def walk(node):
                    if isinstance(node,dict):
                        for key,value in node.items():
                            if key in ('code','errorType') and isinstance(value,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}',value): codes.add(value)
                            walk(value)
                    elif isinstance(node,list):
                        for value in node:walk(value)
                    elif isinstance(node,str):
                        for field in ('startDate','targetingType','expressionType','expression','dynamicBidding','sku','asin','budget','bid'):
                            if field in node:fields.add(field)
                walk(value)
                return {'api_error_codes':sorted(codes),'api_error_fields':sorted(fields)}
            try:
                response=self.inner.post(endpoint,body,*args,**kwargs)
                key={'/sp/campaigns':'campaigns','/sp/adGroups':'adGroups','/sp/productAds':'productAds','/sp/targets':'targetingClauses'}[endpoint]
                if not batch_outcome(response,key,1)['success']:self.last_error=summarize(response)
                return response
            except Exception as exc:
                response=getattr(exc,'response',None)
                if response is not None:
                    try:self.last_error=summarize(response.json())
                    except (ValueError,TypeError):pass
                raise
        def put(self,endpoint,body,*args,**kwargs):
            rec=store.read().get('intents',{}).get(ASIN,{})
            rows=body.get('campaigns',[]) if endpoint=='/sp/campaigns' else []
            if len(rows)!=1 or str(rows[0].get('campaignId'))!=str(rec.get('campaign_id')) or set(rows[0])!={'campaignId','state'}:
                raise RuntimeError('Unexpected probe update')
            if rows[0]['state'] not in ('ENABLED','PAUSED'):raise RuntimeError('Unsupported probe state')
            if rows[0]['state']=='ENABLED':
                campaign=next((c for c in self.inner.list_campaigns() if str(c['campaignId'])==str(rec['campaign_id'])),None)
                if not campaign or campaign.get('startDate')!=rec['start_date'] or rec['start_date']<=datetime.now(ET).date().isoformat():
                    raise RuntimeError('Activation requires verified future start date')
            return self.inner.put(endpoint,body,*args,**kwargs)
    wrapped=ProbeClient(client)
    adapter=ProductionLaunchAmazon(wrapped)
    guard=ProductionLaunchGuard(wrapped)

    def cleanup():
        rec=store.read().get('intents',{}).get(ASIN)
        if not rec:return True
        found=[c for c in client.list_campaigns() if c.get('name')==rec['launch_name']]
        if not found:return False if rec.get('campaign_id') else True
        verified=True
        # Restrictive state-only writes may pause all exact-name matches after
        # an ambiguous create, but never adopt a fuzzy title or ASIN match.
        for campaign in found:
            cid=str(campaign['campaignId'])
            if campaign.get('state')!='PAUSED':
                response=client.put('/sp/campaigns',{'campaigns':[{'campaignId':cid,'state':'PAUSED'}]})
                if not batch_outcome(response,'campaigns',1)['success']:verified=False
            actual=[c for c in client.list_campaigns() if str(c['campaignId'])==cid]
            verified=verified and len(actual)==1 and actual[0].get('state')=='PAUSED'
        return verified

    if cleanup_only:
        ok=cleanup()
        print('LIVE_LAUNCH_CLEANUP '+json.dumps({'final_paused_verified':ok}))
        return 0 if ok else 1

    # Only one probe process may advance this deterministic test at a time.
    with GCSState(NAMESPACE+'-runner').locked():
        saved=store.read()
        if saved.get('probe_complete'):
            saved_result=saved['result']
            saved_result['final_paused_verified']=cleanup()
            saved_result['success']=saved_result['success'] and saved_result['final_paused_verified']
            print('CONTROLLED_LIVE_LAUNCH '+json.dumps(saved_result))
            return 0 if saved_result['success'] else 1
        try:
            policy=Policy.model_validate(GCSState('profit-policy').read()['policy'])
            if policy.daily_limit>75:raise LaunchBlocked('Unexpected account spending limit')
            product=GCSState('profit-data').read().get('products',{}).get(ASIN)
            if not product or ASIN not in policy.product_exploration:raise LaunchBlocked('Verified opted-in product required')
            rec=saved.get('intents',{}).get(ASIN)
            decision=guard.authorize_paused_launch({**product,**({'campaign_id':rec['campaign_id']} if rec and rec.get('campaign_id') else {})},policy.model_dump(),wrapped.profile_id)
            if decision['budget_cap']>2 or decision['max_bid']>.07:raise LaunchBlocked('Probe exceeds observed approved bounds')
            result.update(budget=decision['budget_cap'],max_bid=decision['max_bid'])
            if rec is None:
                rec={'status':'reserved','launch_name':'NWS VALIDATION | 20261016 PR47 AUTO 1',
                     'profile':str(wrapped.profile_id),'sku':product['sku'],
                     'start_date':(datetime.now(ET).date()+timedelta(days=1)).isoformat()}
                saved={'intents':{ASIN:rec},'unresolved':ASIN}
                store.save(saved)
                # Intent is durable before the only non-idempotent campaign POST.
                response=adapter.create_paused_campaign({**product,**decision,
                    'launch_name':rec['launch_name'],'start_date':rec['start_date']},wrapped.profile_id)
                rec.update(campaign_id=response['campaign_id'],status='paused_verified')
                store.save(saved)
            elif not rec.get('campaign_id'):
                found=[c for c in client.list_campaigns() if c.get('name')==rec['launch_name']]
                if len(found)!=1:raise LaunchBlocked('Campaign creation uncertain; no POST replay')
                rec.update(campaign_id=str(found[0]['campaignId']),status='paused_verified')
                store.save(saved)
            cid=rec['campaign_id']
            result['campaign_created']=True
            adapter.launch_names[ASIN]=rec['launch_name']
            adapter.expected_skus[ASIN]=product['sku']
            if not adapter.confirm_paused_campaign(cid,ASIN,wrapped.profile_id):raise LaunchBlocked('Paused readback failed')
            PausedProvisioner(store=store,adapter=adapter).provision(cid=cid,asin=ASIN,sku=product['sku'],
                profile=wrapped.profile_id,bid=decision['max_bid'])
            result['provisioned']=True
            activated=continue_launch(store=store,adapter=adapter,guard=guard,asin=ASIN,
                profile=wrapped.profile_id,sku=product['sku'],bid=decision['max_bid'],policy=policy.model_dump())
            result['activation_verified']=activated['status']=='enabled_verified'
        except Exception as exc:
            result['error_type']=type(exc).__name__
            result.update(wrapped.last_error)
            # Safe diagnostic codes only; no Amazon messages, product costs or credentials.
            response=getattr(exc,'response',None)
            if response is not None:result['http_status']=response.status_code
            if isinstance(exc,LaunchBlocked):
                reasons=('Paused readback failed','Probe exceeds observed approved bounds',
                    'Amazon response unconfirmed at ad_group','Amazon response unconfirmed at product_ad',
                    'Amazon response unconfirmed at auto_target','Product ad identity mismatch',
                    'Unexpected ad group in reserved campaign','Automatic targeting identity mismatch')
                result['reason_code']=str(exc) if str(exc) in reasons else 'guard_or_readback_hold'
        finally:
            try:result['final_paused_verified']=cleanup()
            except Exception as exc:result['cleanup_error_type']=type(exc).__name__
            result['success']=result['campaign_created'] and result['provisioned'] and result['activation_verified'] and result['final_paused_verified']
            latest=store.read()
            latest['result']=result
            latest['probe_complete']=result['success']
            store.save(latest)
        print('CONTROLLED_LIVE_LAUNCH '+json.dumps(result))
        summary=os.getenv('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary,'a') as f:f.write('## Controlled live campaign lifecycle\n```json\n'+json.dumps(result,indent=2)+'\n```\n')
        return 0 if result['success'] else 1

if __name__=='__main__':
    try:sys.exit(run('--cleanup' in sys.argv))
    except Exception as exc:
        print('CONTROLLED_LIVE_LAUNCH_SETUP_FAILED '+type(exc).__name__)
        sys.exit(1)
