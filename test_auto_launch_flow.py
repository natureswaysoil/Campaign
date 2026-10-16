import unittest
from contextlib import contextmanager
from auto_launch_flow import continue_launch, reconcile_readonly
from auto_launch_coordinator import LaunchBlocked
class Store:
    def __init__(self,status):
        self.data={"intents":{"B001":{"status":status,"campaign_id":"42"}},"unresolved":"B001"}
    @contextmanager
    def locked(self): yield self.data
    def save(self,value):pass
class Guard:
    def authorize_paused_launch(self,*args):pass
class Adapter:
    def __init__(self,fail=False):self.fail=fail;self.calls=0
    def verify_provisioning(self,**kwargs):return True
    def confirm_paused_campaign(self,*args):return True
    def activate_verified(self,**kwargs):
        self.calls+=1
        if self.fail: raise TimeoutError("response lost")
        return {"status":"enabled_verified"}
class FlowTests(unittest.TestCase):
    def run_flow(self,store,adapter):
        return continue_launch(store=store,adapter=adapter,guard=Guard(),asin="B001",
            profile="123",sku="SKU",bid=.3,policy={"daily_limit":75})
    def test_paused_waits_for_provisioning(self):
        a=Adapter();self.assertEqual(self.run_flow(Store("paused_verified"),a)["status"],"ready_for_provisioning")
        self.assertEqual(a.calls,0)
    def test_verified_activation(self):
        s=Store("provisioned_verified")
        self.assertEqual(self.run_flow(s,Adapter())["status"],"enabled_verified")
        self.assertIsNone(s.data["unresolved"])
    def test_timeout_blocks_replay(self):
        s=Store("provisioned_verified");a=Adapter(fail=True)
        with self.assertRaises(TimeoutError):self.run_flow(s,a)
        with self.assertRaises(LaunchBlocked):self.run_flow(s,a)
        self.assertEqual(a.calls,1)
        self.assertTrue(reconcile_readonly(store=s,adapter=a,asin="B001",profile="123")["requires_review"])
if __name__=="__main__":unittest.main()
