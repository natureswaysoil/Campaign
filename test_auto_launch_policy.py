import unittest
from auto_launch_policy import eligible_auto_launch

class AutoLaunchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.products = [{"asin":"B001","product_id":"one"},{"asin":"B002","product_id":"two"}]
        self.verified = {"B001":{"asin":"B001","sku":"s1","price":29.99,"cogs":7,"shipping":5,"fees":5,"available":20},
                         "B002":{"asin":"B002","sku":"s2","price":29.99,"cogs":7,"shipping":5,"fees":5,"available":20}}
    def test_prevents_duplicates_and_limits_launches(self):
        self.assertEqual(len(eligible_auto_launch(self.products,self.verified,set(),{"profit_reserve":.10})),1)
        self.assertEqual(eligible_auto_launch(self.products,self.verified,{"B001","B002"},{"profit_reserve":.10}),[])
    def test_rejects_missing_economics_and_stock(self):
        self.verified["B001"].pop("fees")
        self.verified["B002"]["available"]=0
        self.assertEqual(eligible_auto_launch(self.products,self.verified,set(),{"profit_reserve":.10}),[])
    def test_rejects_loss_products(self):
        self.verified["B001"]["cogs"]=28
        self.assertEqual(eligible_auto_launch(self.products,self.verified,{"B002"},{"profit_reserve":.10}),[])
    def test_requires_policy(self):
        with self.assertRaises(ValueError): eligible_auto_launch(self.products,self.verified,set(),None)

if __name__ == "__main__": unittest.main()
