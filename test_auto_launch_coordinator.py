import unittest
from contextlib import contextmanager
from auto_launch_coordinator import AutoLaunchCoordinator, LaunchBlocked

P={"asin":"B001","product_id":"bone"}
V={"B001":{"asin":"B001","sku":"SKU","price":29.99,"cogs":7,"shipping":5,"fees":5,"available":20}}
POL={"daily_limit":75,"profit_reserve":.1}

class Store:
    def __init__(self): self.data={}
    @contextmanager
    def locked(self): yield self.data
    def save(self,x): self.data=dict(x)

class Amazon:
    def __init__(self): self.calls=0;self.confirm=True
    def list_advertised_asins(self,p):return set()
    def create_paused_campaign(self,c,p):self.calls+=1;return {"campaign_id":"99"}
    def confirm_paused_campaign(self,c,a,p):return self.confirm

class Guard:
    def authorize_paused_launch(self,*args):pass

class CoordinatorTests(unittest.TestCase):
    def setUp(self):self.store=Store();self.amazon=Amazon();self.guard=Guard()
    def launch(self,enable=True):
        return AutoLaunchCoordinator(store=self.store,amazon=self.amazon,guard=self.guard,enable=enable).run_one(
            products=[P],verified=V,policy=POL,account_profile="123")
    def test_disabled_by_default(self):
        self.assertEqual(self.launch(False)["status"],"disabled")
        self.assertEqual(self.amazon.calls,0)
    def test_paused_only_then_pending(self):
        self.assertEqual(self.launch()["status"],"paused_requires_activation")
        self.assertEqual(self.amazon.calls,1)
        with self.assertRaises(LaunchBlocked):self.launch()
        self.assertEqual(self.amazon.calls,1)
    def test_uncertain_never_retries(self):
        self.amazon.confirm=False
        with self.assertRaises(LaunchBlocked):self.launch()
        with self.assertRaises(LaunchBlocked):self.launch()
        self.assertEqual(self.amazon.calls,1)
    def test_missing_inventory_no_write(self):
        V2={"B001":{**V["B001"],"available":0}}
        r=AutoLaunchCoordinator(store=self.store,amazon=self.amazon,guard=self.guard,enable=True).run_one(
            products=[P],verified=V2,policy=POL,account_profile="123")
        self.assertEqual(r["created"],0)
        self.assertEqual(self.amazon.calls,0)
    def test_guard_failure_keeps_reservation(self):
        class Deny:
            def authorize_paused_launch(self,*args):raise LaunchBlocked("deny")
        c=AutoLaunchCoordinator(store=self.store,amazon=self.amazon,guard=Deny(),enable=True)
        with self.assertRaises(LaunchBlocked):c.run_one(products=[P],verified=V,policy=POL,account_profile="123")
        self.assertEqual(self.amazon.calls,0)
        self.assertEqual(self.store.data["unresolved"],"B001")

if __name__=="__main__":unittest.main()
