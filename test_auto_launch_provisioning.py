import unittest
from contextlib import contextmanager
from auto_launch_provisioning import PausedProvisioner
from auto_launch_coordinator import LaunchBlocked
class Store:
    def __init__(self):
        self.state={"intents":{"B001":{"campaign_id":"42","status":"paused_verified"}},"unresolved":"B001"}
    @contextmanager
    def locked(self): yield self.state
    def save(self,x): pass
class Client:
    def __init__(self):self.calls=[]
    def post(self,endpoint,payload):
        self.calls.append(endpoint)
        if endpoint=="/sp/adGroups":
            return {"adGroups":{"success":[{"adGroup":{"adGroupId":"7"}}]}}
        raise TimeoutError("Amazon acknowledgement lost")
class Adapter:
    def __init__(self):self.client=Client()
    def _profile(self,profile):pass
    def confirm_paused_campaign(self,*args):return True
    def verify_provisioning(self,**kwargs):return False
class Tests(unittest.TestCase):
    def test_unknown_ack_blocks_replay(self):
        store=Store();adapter=Adapter();p=PausedProvisioner(store=store,adapter=adapter)
        args=dict(cid="42",asin="B001",sku="SKU",profile="123",bid=.3)
        with self.assertRaises(TimeoutError):p.provision(**args)
        self.assertEqual(store.state["intents"]["B001"]["status"],"manual_reconciliation_required")
        with self.assertRaises(LaunchBlocked):p.provision(**args)
        self.assertEqual(adapter.client.calls,["/sp/adGroups","/sp/productAds"])
    def test_missing_intent_fails_before_write(self):
        store=Store();adapter=Adapter()
        with self.assertRaises(LaunchBlocked):PausedProvisioner(store=store,adapter=adapter).provision(
            cid="45",asin="B001",sku="SKU",profile="123",bid=.3)
        self.assertFalse(adapter.client.calls)
if __name__=="__main__":unittest.main()
