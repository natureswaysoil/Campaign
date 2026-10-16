import json
import unittest
from tenant_context import TenantContext, TenantConfigurationError
from tenant_auth import TenantBinding, TenantRegistry
from tenant_credentials import load_tenant_ads_credentials

class TenantRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.a = TenantBinding("service-account-a", TenantContext("farm_a", "123"), "projects/demo/secrets/farm-a/versions/latest")
        self.b = TenantBinding("service-account-b", TenantContext("farm_b", "456"), "projects/demo/secrets/farm-b/versions/latest")
        self.registry = TenantRegistry((self.a, self.b))

    def test_authenticated_resolution(self):
        self.assertEqual(self.registry.resolve("service-account-a").context.tenant_id, "farm_a")
        for principal in (None, "", "unknown", "farm_b"):
            with self.subTest(principal=principal), self.assertRaises(TenantConfigurationError):
                self.registry.resolve(principal)
        with self.assertRaises(TenantConfigurationError):
            self.registry.resolve("service-account-a", requested_profile="456")

    def test_duplicate_bindings_rejected(self):
        with self.assertRaises(TenantConfigurationError):
            TenantRegistry((self.a, self.a))
        alias = TenantBinding("another-service", self.a.context, "projects/demo/secrets/x/versions/latest")
        with self.assertRaises(TenantConfigurationError):
            TenantRegistry((self.a, alias))

    def test_secret_identity_verification(self):
        class Client:
            class Payload:
                data = json.dumps({"client_id":"id","client_secret":"secret","refresh_token":"token","ads_profile_id":"123"}).encode()
            class Response:
                payload = None
            def access_secret_version(self, request):
                self.name = request["name"]
                return type("Response", (), {"payload": self.Payload()})()
        client = Client()
        result = load_tenant_ads_credentials(self.a, client=client)
        self.assertEqual(result["client_id"], "id")
        self.assertEqual(client.name, self.a.secret_resource)
        with self.assertRaises(TenantConfigurationError):
            load_tenant_ads_credentials(self.b, client=client)

if __name__ == "__main__":
    unittest.main()
