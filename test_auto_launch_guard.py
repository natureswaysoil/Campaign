import unittest
from unittest.mock import patch
from auto_launch_coordinator import LaunchBlocked
from auto_launch_guard import ProductionLaunchGuard
class Client: profile_id="123"
class PreflightTests(unittest.TestCase):
    def test_account_mismatch_fails_before_storage_access(self):
        with self.assertRaises(LaunchBlocked):
            ProductionLaunchGuard(Client()).authorize_paused_launch({"asin":"B000000001"},{"daily_limit":75},"999")
    def test_missing_policy_blocks(self):
        class Store:
            def __init__(self,name): pass
            def read(self): return {}
        with patch("auto_launch_guard.GCSState",Store):
            with self.assertRaises(LaunchBlocked):
                ProductionLaunchGuard(Client()).authorize_paused_launch({"asin":"B000000001"},{"daily_limit":75},"123")
if __name__=="__main__":unittest.main()
