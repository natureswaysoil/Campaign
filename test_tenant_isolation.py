import sys
import types
import unittest
from tenant_context import TenantContext, TenantConfigurationError
from tenant_bigquery import TenantBigQuery
from tenant_isolation import TenantNamespace

class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.a = TenantContext("farm_a", "12345", dataset_id="ppc_farm_a")
        self.b = TenantContext("farm_b", "67890", dataset_id="ppc_farm_b")

    def test_bq_dataset_isolation(self):
        class Dummy: pass
        left = TenantBigQuery(self.a, "amazon-ppc-1234", Dummy())
        right = TenantBigQuery(self.b, "amazon-ppc-1234", Dummy())
        self.assertNotEqual(left.table_id("metrics"), right.table_id("metrics"))
        self.assertIn("ppc_farm_a", left.table_id("metrics"))

    def test_invalid_query_identifiers_fail_closed(self):
        class Dummy: pass
        db = TenantBigQuery(self.a, "amazon-ppc-1234", Dummy())
        for invalid in ("../metrics", "other.metrics", "metrics;DROP", ""):
            with self.subTest(invalid=invalid), self.assertRaises(TenantConfigurationError):
                db.table_id(invalid)
        with self.assertRaises(TenantConfigurationError):
            TenantBigQuery(TenantContext("missing", "12"), "amazon-ppc-1234", Dummy())
        with self.assertRaises(TenantConfigurationError):
            TenantBigQuery(self.a, "bad", Dummy())

    def test_parameterized_read_limit(self):
        class Job:
            def result(self): return [{"value": 1}]
        class Client:
            def query(self, statement, **kwargs):
                self.statement = statement
                self.kwargs = kwargs
                return Job()
        class Scalar:
            def __init__(self, name, kind, value): self.name, self.kind, self.value = name, kind, value
        class Config:
            def __init__(self, query_parameters): self.query_parameters = query_parameters
        previous = {name: sys.modules.get(name) for name in ("google", "google.cloud", "google.cloud.bigquery")}
        try:
            google = types.ModuleType("google")
            cloud = types.ModuleType("google.cloud")
            bigquery = types.ModuleType("google.cloud.bigquery")
            bigquery.QueryJobConfig, bigquery.ScalarQueryParameter = Config, Scalar
            google.cloud = cloud
            cloud.bigquery = bigquery
            sys.modules.update({"google": google, "google.cloud": cloud, "google.cloud.bigquery": bigquery})
            client = Client()
            rows = TenantBigQuery(self.a, "amazon-ppc-1234", client).read_rows("metrics", limit=5)
            self.assertEqual(rows[0]["value"], 1)
            self.assertIn("@row_limit", client.statement)
            self.assertEqual(client.kwargs["job_config"].query_parameters[0].value, 5)
        finally:
            for name, value in previous.items():
                if value is None: sys.modules.pop(name, None)
                else: sys.modules[name] = value

    def test_storage_and_profile_isolation(self):
        self.assertNotEqual(TenantNamespace(self.a).key("state","profit_data"),
                            TenantNamespace(self.b).key("state","profit_data"))
        with self.assertRaises(TenantConfigurationError):
            TenantNamespace(self.a).assert_profile("67890")
        with self.assertRaises(TenantConfigurationError):
            TenantNamespace(self.a).key("lock", "../escape")

    def test_no_invalid_limits(self):
        class Dummy: pass
        db = TenantBigQuery(self.a, "amazon-ppc-1234", Dummy())
        for value in (0, 10001, True, -1, "5"):
            with self.subTest(value=value), self.assertRaises(TenantConfigurationError):
                db.read_rows("metrics", limit=value)

if __name__ == "__main__": unittest.main()
