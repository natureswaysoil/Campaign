"""Guarded integration tests: durable snapshots, real profit guard, fake Amazon."""
import copy
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import profit_guard
import profit_intelligence
import launch_preflight
import auto_launch_guard
from profit_intelligence import Policy, ET, cpc_ceiling, conversion_evidence
from profit_guard import GuardBlocked, apply_isolated_bids
from auto_launch_production import ProductionLaunchAmazon
from auto_launch_coordinator import AutoLaunchCoordinator, LaunchBlocked
from auto_launch_guard import ProductionLaunchGuard
from auto_launch_provisioning import PausedProvisioner
from auto_launch_pipeline import run_pipeline

class Memory:
    objects = {}
    locks = set()
    def __init__(self, name): self.name = name
    def read(self): return copy.deepcopy(self.objects.get(self.name, {}))
    def save(self, value): self.objects[self.name] = copy.deepcopy(value)
    @contextmanager
    def locked(self):
        if self.name in self.locks: raise RuntimeError('Nested/concurrent lock')
        self.locks.add(self.name)
        try: yield self.read()
        finally: self.locks.remove(self.name)

@pytest.fixture
def state(monkeypatch):
    Memory.objects, Memory.locks = {}, set()
    for module in (profit_guard, profit_intelligence, launch_preflight, auto_launch_guard):
        monkeypatch.setattr(module, 'GCSState', Memory)
    now = time.time()
    p = {'asin':'B012345678','sku':'soil','price':40.,'cogs':15.,'fees':6.,'shipping':10.,
         'other_cost':0.,'updated_at':now,'market_at':now,'inventory':100,'inventory_at':now,
         'daily_units':2.,'velocity_at':now,'lead_days':7}
    policy = Policy(daily_limit=75, product_exploration={p['asin']:{'cvr':.01,'daily_budget':2}})
    Memory('profit-data').save({'products':{p['asin']:p},'metrics':[], 'complete_spend_at':now})
    Memory('profit-policy').save({'policy':policy.model_dump()})
    return now, p, policy

def test_mature_click_boundary_and_freshness(state):
    now, p, _ = state
    policy = Policy(daily_limit=75)
    day = (datetime.fromtimestamp(now,ET).date()-timedelta(days=15)).isoformat()
    row = {'date':day,'observed_at':now,'grain':'campaign','campaign_id':'1',
           'clicks':29,'orders':2,'sales':80,'spend':5}
    with pytest.raises(ValueError, match='insufficient_mature'):
        cpc_ceiling(p,[row],policy,now)
    row['clicks']=30
    assert cpc_ceiling(p,[row],policy,now)['source']=='mature_target_metrics'
    data={'metrics':[row]}
    assert conversion_evidence(data,policy,now,grain='campaign',campaign_id='1')['eligible_clicks']==30
    row['observed_at']=now-36*3600-1
    assert conversion_evidence(data,policy,now,grain='campaign',campaign_id='1')['eligible_clicks']==0
    row.update(observed_at=now,date=(datetime.fromtimestamp(now,ET).date()-timedelta(days=14)).isoformat())
    assert conversion_evidence(data,policy,now,grain='campaign',campaign_id='1')['eligible_clicks']==0

def test_isolated_holds_do_not_hide_accept_reject_or_uncertain():
    client=MagicMock()
    client.put.side_effect=[GuardBlocked('insufficient_mature_conversion_data'),
        {'keywords':{'success':[{'keywordId':'2'}]}},
        {'keywords':{'error':[{'code':'INVALID_ARGUMENT'}]}}, TimeoutError()]
    result=apply_isolated_bids(client,'/sp/keywords',[{'keywordId':str(i),'bid':.1} for i in range(4)])
    assert {k:result[k] for k in ('blocked','accepted','rejected','unconfirmed')}==dict(blocked=1,accepted=1,rejected=1,unconfirmed=1)
    assert result['success'] is False
    assert client.put.call_count==4

class Amazon:
    profile_id='123'
    profit_product_asin=None
    def __init__(self, fail=None):
        self.campaigns=[];self.groups=[];self.ads=[];self.targets=[];self.writes=[];self.fail=fail
    def list_campaigns(self): return copy.deepcopy(self.campaigns)
    def list_product_ads(self,cid):return copy.deepcopy([a for a in self.ads if a['campaignId']==str(cid)])
    def list_keywords(self,cid):return []
    def list_targets(self,cid):return copy.deepcopy([t for t in self.targets if t['campaignId']==str(cid)])
    def list_all(self,path,key,filters=None):
        data={'adGroups':self.groups,'productAds':self.ads,'targetingClauses':self.targets,'keywords':[]}[key]
        ids=(filters or {}).get('campaignIdFilter',{}).get('include')
        return copy.deepcopy([x for x in data if not ids or x['campaignId'] in ids])
    def post(self,path,body):return profit_guard.guarded_write(self,'POST',path,body,lambda b:self.send('POST',path,b))
    def put(self,path,body):return profit_guard.guarded_write(self,'PUT',path,body,lambda b:self.send('PUT',path,b))
    def send(self,method,path,body):
        key,id_key=profit_guard.ENDPOINTS[path]
        self.writes.append((method,path))
        objects={'campaigns':self.campaigns,'adGroups':self.groups,'productAds':self.ads,'targetingClauses':self.targets}[key]
        rows=[]
        for row in body[key]:
            if method=='POST':
                row={**row,id_key:str(len(objects)+1)}
                objects.append(copy.deepcopy(row))
            else:
                found=next(x for x in objects if x[id_key]==row[id_key]);found.update(row)
            rows.append({id_key:row[id_key]})
        if self.fail==(method,path):
            self.fail=None
            raise TimeoutError('Acknowledgement lost after Amazon committed')
        return {key:{'success':rows}}

def execute(client,p,policy):
    store=Memory('auto-launch')
    adapter=ProductionLaunchAmazon(client)
    guard=ProductionLaunchGuard(client)
    coordinator=AutoLaunchCoordinator(store=store,amazon=adapter,guard=guard,enable=True)
    return run_pipeline(coordinator=coordinator,provisioner=PausedProvisioner(store=store,adapter=adapter),
        store=store,adapter=adapter,guard=guard,products=[{'asin':p['asin']}],
        verified={p['asin']:{**p,'available':100}},policy=policy.model_dump(),account_profile='123')

@pytest.mark.parametrize('failure',[None,('POST','/sp/campaigns'),('POST','/sp/adGroups'),
    ('POST','/sp/productAds'),('POST','/sp/targets'),('PUT','/sp/campaigns')])
def test_restart_reconciles_every_committed_stage_without_duplicates(state,failure):
    _,p,policy=state
    client=Amazon(failure)
    if failure:
        with pytest.raises(TimeoutError):execute(client,p,policy)
    result=execute(client,p,policy)
    assert result['status']=='enabled_verified'
    assert len(client.campaigns)==len(client.groups)==len(client.ads)==len(client.targets)==1
    assert client.campaigns[0]['state']=='ENABLED'
    assert client.groups[0]['defaultBid']<=.05
    assert client.writes.count(('POST','/sp/campaigns'))==1
    assert client.writes.count(('POST','/sp/adGroups'))==1
    assert client.writes.count(('POST','/sp/productAds'))==1
    assert client.writes.count(('POST','/sp/targets'))==1
    assert client.writes.count(('PUT','/sp/campaigns'))==1
    assert Memory('auto-launch').read()['unresolved'] is None

def test_uncertain_absent_entity_never_replays_post(state):
    _,p,policy=state
    client=Amazon(('POST','/sp/productAds'))
    with pytest.raises(TimeoutError):execute(client,p,policy)
    client.ads=[]  # An empty/lagged read is not proof of safe retry.
    with pytest.raises(LaunchBlocked):execute(client,p,policy)
    assert client.writes.count(('POST','/sp/productAds'))==1

def test_blocked_launch_creates_no_campaign_or_uncertain_reservation(state):
    _,p,policy=state
    policy=Policy(daily_limit=75)
    Memory('profit-policy').save({'policy':policy.model_dump()})
    client=Amazon()
    with pytest.raises(LaunchBlocked):execute(client,p,policy)
    assert client.writes==[]
    assert Memory('auto-launch').read().get('unresolved') is None

def test_preview_and_live_preflight_do_not_create_shell_without_conversion(state):
    _,p,_=state
    Memory('profit-policy').save({'policy':Policy(daily_limit=75).model_dump()})
    client=Amazon()
    with pytest.raises(ValueError,match='insufficient_mature'):
        launch_preflight.launch_preflight(client,asin=p['asin'],sku=p['sku'],
            plans=[{'type':'AUTO','budget':2,'bid':.5}])
    assert client.writes==[]

def test_real_guard_preserves_reduction_when_other_increase_is_held(state):
    _,p,_=state
    Memory('profit-policy').save({'policy':Policy(daily_limit=75).model_dump()})
    client=Amazon()
    client.campaigns=[{'campaignId':'1','state':'ENABLED','budget':{'budget':2,'budgetType':'DAILY'}}]
    client.groups=[{'campaignId':'1','adGroupId':i,'state':'ENABLED','defaultBid':.2} for i in ('1','2')]
    client.ads=[{'campaignId':'1','adGroupId':'1','asin':p['asin'],'sku':p['sku'],'state':'ENABLED'}]
    result=apply_isolated_bids(client,'/sp/adGroups',[
        {'adGroupId':'1','defaultBid':.4},{'adGroupId':'2','defaultBid':.1}])
    assert result['status']=='safety_hold' and result['success']
    assert result['blocked']==result['accepted']==1
    assert client.groups[0]['defaultBid']==.2
    assert client.groups[1]['defaultBid']==.1
    assert client.writes==[('PUT','/sp/adGroups')]

def test_budget_envelope_is_checked_before_campaign_creation(state):
    _,p,policy=state
    client=Amazon()
    client.campaigns=[{'campaignId':'old','state':'ENABLED','budget':{'budget':75,'budgetType':'DAILY'}}]
    client.ads=[{'campaignId':'old','asin':'BOTHER0001','state':'ENABLED'}]
    with pytest.raises(GuardBlocked,match='Aggregate'):
        execute(client,p,policy)
    assert not client.writes

def test_cost_schedule_preserves_existing_unless_explicitly_updated(state,monkeypatch):
    import automation_store, verified_costs, decision_sync
    _,p,_=state
    monkeypatch.setattr(automation_store,'GCSState',Memory)
    monkeypatch.setattr(verified_costs,'MATCHES',{p['asin']:(p['sku'],'32oz')})
    schedule={'verified_at':p['updated_at']+.01,'sizes':{size:[7,8] for size in ('32oz','1gal','2.5gal')}}
    refresh=MagicMock(side_effect=lambda client,candidate,**kw: {**candidate,'market_at':time.time()})
    monkeypatch.setattr(decision_sync,'refresh_product_market',refresh)
    assert verified_costs.seed_products(object(),schedule)['existing_preserved']==1
    refresh.assert_not_called()
    assert verified_costs.seed_products(object(),schedule,update_existing=True)['existing_updated']==1
    saved=Memory('profit-data').read()['products'][p['asin']]
    assert (saved['cogs'],saved['shipping'])==(7,8)
    assert saved['updated_at']==schedule['verified_at']
    assert saved['other_cost']==p['other_cost']

def test_monitor_safety_hold_is_not_an_execution_failure():
    from scripts.monitor_automation import summarize
    workflows={kind:{'last_result':{'success':True}} for kind in ('bid','harvest','opportunity','sales-growth','profit')}
    workflows['bid']['last_result'].update(status='safety_hold',bid_outcomes={'blocked':2,'accepted':1,'rejected':0,'unconfirmed':0})
    assert summarize({'123':workflows})['alerts']==[]
    workflows['bid']['last_result'].update(success=False,status='execution_failure')
    assert 'bid latest apply reported failure' in summarize({'123':workflows})['alerts']

def test_invalid_runtime_settings_fail_closed(monkeypatch):
    from optimizer_settings import bid_settings
    monkeypatch.setenv('PROTECT_BID_MULTIPLIER','nan')
    with pytest.raises(ValueError):bid_settings()

def test_campaign_conversions_do_not_authorize_new_target(state):
    now,p,_=state
    policy=Policy(daily_limit=75)
    client=Amazon()
    client.campaigns=[{'campaignId':'1','state':'ENABLED','budget':{'budget':2}}]
    client.groups=[{'campaignId':'1','adGroupId':'1','state':'ENABLED','defaultBid':.1}]
    client.ads=[{'campaignId':'1','adGroupId':'1','asin':p['asin'],'sku':p['sku'],'state':'ENABLED'}]
    client.targets=[{'campaignId':'1','adGroupId':'1','targetId':'t','state':'ENABLED','bid':.1}]
    data=Memory('profit-data').read()
    data['metrics']=[{'grain':'campaign','campaign_id':'1','date':(datetime.fromtimestamp(now,ET).date()-timedelta(days=20)).isoformat(),
        'observed_at':now,'clicks':100,'orders':10,'sales':400,'spend':10}]
    check=profit_guard.bid_preflight(client,'/sp/targets',{'targetId':'t','bid':.2},data=data,policy=policy,now=now)
    assert check['status']=='blocked'
    assert check['evidence']['eligible_clicks']==0
    assert check['evidence']['filters']['target_id']=='t'

def test_unknown_activation_paused_is_not_replayed(state):
    _,p,policy=state
    client=Amazon(('PUT','/sp/campaigns'))
    with pytest.raises(TimeoutError):execute(client,p,policy)
    client.campaigns[0]['state']='PAUSED'
    with pytest.raises(LaunchBlocked,match='Activation outcome uncertain'):
        execute(client,p,policy)
    assert client.writes.count(('PUT','/sp/campaigns'))==1

def test_ambiguous_group_does_not_create_replacement(state):
    _,p,policy=state
    client=Amazon(('POST','/sp/adGroups'))
    with pytest.raises(TimeoutError):execute(client,p,policy)
    client.groups.append({**client.groups[0],'adGroupId':'another'})
    with pytest.raises(LaunchBlocked):execute(client,p,policy)
    assert client.writes.count(('POST','/sp/adGroups'))==1

def test_saved_dashboard_target_tightens_production_winner_threshold(state,monkeypatch):
    import automation_store
    from optimizer_settings import winner_acos_limit
    monkeypatch.setenv('AUTOMATION_STATE_BUCKET','offline-test')
    monkeypatch.setattr(automation_store,'GCSState',Memory)
    assert winner_acos_limit(.35)==.25


def test_legacy_empty_campaign_blocks_new_automatic_shell(state):
    _,p,policy=state
    client=Amazon()
    client.campaigns=[{'campaignId':'orphan','state':'PAUSED','name':'Old launch','budget':{'budget':2}}]
    with pytest.raises(LaunchBlocked,match='review incomplete launch'):
        execute(client,p,policy)
    assert not client.writes
