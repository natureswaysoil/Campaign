import unittest
from auto_launch_production import ProductionLaunchAmazon
from auto_launch_coordinator import LaunchBlocked
class Client:
    profile_id="123"
    def __init__(self):self.writes=[];self.state="PAUSED"
    def list_campaigns(self):return [{"campaignId":"42","name":"NWS AUTO PENDING | B001","state":self.state,"targetingType":"AUTO"}]
    def list_product_ads(self, cid):return [{"asin":"B001","adGroupId":"7","state":"ENABLED"}]
    def list_all(self, endpoint, key, filters=None):return [{"campaignId":"42","adGroupId":"7","state":"ENABLED"}]
    def list_targets(self,cid):
        from auto_targets import AUTO_EXPRESSIONS
        return [{'campaignId':'42','adGroupId':'7','state':'ENABLED','expressionType':'AUTO',
                 'expression':[{'type':kind}],'targetId':str(i)} for i,kind in enumerate(sorted(AUTO_EXPRESSIONS))]
    def post(self, endpoint, body):
        self.writes.append((endpoint,body))
        return {"campaigns":{"success":[{"campaign":{"campaignId":"42"}}]}}
    def put(self, endpoint, body):
        self.writes.append((endpoint,body))
        self.state="ENABLED"
        return {"campaigns":{"success":[{"campaignId":"42"}]}}
class AdapterTests(unittest.TestCase):
    def test_paused_first_and_readback(self):
        c=Client();a=ProductionLaunchAmazon(c)
        self.assertEqual(a.create_paused_campaign({"asin":"B001","budget_cap":3.0},"123")["campaign_id"],"42")
        self.assertEqual(c.writes[0][1]["campaigns"][0]["state"],"PAUSED")
        self.assertTrue(a.confirm_paused_campaign("42","B001","123"))
    def test_profile_mismatch_blocks(self):
        c=Client();a=ProductionLaunchAmazon(c)
        with self.assertRaises(LaunchBlocked):a.create_paused_campaign({"asin":"B001"},"456")
        self.assertEqual(c.writes,[])
    def test_activation_requires_independent_provisioning(self):
        c=Client();c.list_product_ads=lambda cid: []
        a=ProductionLaunchAmazon(c)
        with self.assertRaises(LaunchBlocked):
            a.activate_verified(cid="42",asin="B001",profile="123",provisioned=True)
        self.assertEqual(c.writes,[])
    def test_missing_targets_blocks_activation(self):
        c=Client();c.list_targets=lambda cid: []
        with self.assertRaises(LaunchBlocked):ProductionLaunchAmazon(c).activate_verified(cid="42",asin="B001",profile="123")
        self.assertEqual(c.writes,[])
    def test_activation_requires_readback(self):
        c=Client();a=ProductionLaunchAmazon(c)
        self.assertEqual(a.activate_verified(cid="42",asin="B001",profile="123",provisioned=True)["status"],"enabled_verified")
if __name__ == "__main__":unittest.main()
