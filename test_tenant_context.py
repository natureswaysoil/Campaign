"""Offline regression tests for the passive tenant context."""
import unittest
from tenant_context import TenantContext, TenantConfigurationError, tenant_from_env

class TenantContextTests(unittest.TestCase):
    def test_environment_requires_identity(self):
        with self.assertRaises(TenantConfigurationError):
            tenant_from_env({})
    def test_valid_account_context(self):
        ctx = tenant_from_env({"PPC_TENANT_ID":"nature_way","AMAZON_ADS_PROFILE_ID":"12345","AMAZON_ADS_REGION":"na","PPC_BQ_DATASET":"ppc_01"})
        self.assertEqual(ctx.region, "NA")
        ctx.assert_profile("12345")
        self.assertNotIn("ads_profile_id", ctx.safe_log_context())
    def test_blocks_cross_account(self):
        with self.assertRaises(TenantConfigurationError):
            TenantContext("shop", "123").assert_profile("456")
    def test_rejects_invalid_dataset_and_tenant(self):
        for name in ("../other", "a.b", ""):
            with self.subTest(name=name), self.assertRaises(TenantConfigurationError):
                TenantContext(name, "123")
        with self.assertRaises(TenantConfigurationError):
            TenantContext("shop", "123", dataset_id="../leak")
    def test_rejects_invalid_region_profile(self):
        with self.assertRaises(TenantConfigurationError):
            TenantContext("shop", "123", region="OTHER")
        with self.assertRaises(TenantConfigurationError):
            TenantContext("shop", "not-a-number")
if __name__ == "__main__":
    unittest.main()
