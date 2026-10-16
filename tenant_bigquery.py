"""Tenant-scoped BigQuery access for future offline reporting.

Callers must pass an authenticated server-resolved TenantContext. This
component is read-only and does not trigger Amazon Ads writes. Each tenant
must have a dedicated dataset and least-privilege BigQuery service identity.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from tenant_context import TenantContext, TenantConfigurationError

_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")

@dataclass(frozen=True)
class TenantBigQuery:
    context: TenantContext
    project_id: str
    client: object

    def __post_init__(self):
        if not self.context.dataset_id:
            raise TenantConfigurationError("A dedicated tenant dataset is mandatory")
        if not _PROJECT.fullmatch(self.project_id):
            raise TenantConfigurationError("Invalid BigQuery project")
        if self.client is None:
            raise TenantConfigurationError("BigQuery client is required")

    def table_id(self, table: str) -> str:
        if not _TABLE.fullmatch(table):
            raise TenantConfigurationError("Invalid BigQuery table identifier")
        return f"{self.project_id}.{self.context.dataset_id}.{table}"

    def read_rows(self, table: str, *, limit: int = 1000):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
            raise TenantConfigurationError("Invalid query limit")
        # Identifier components are strictly validated; data values always use
        # BigQuery parameters in future predicate-bearing queries.
        query = f"SELECT * FROM `{self.table_id(table)}` LIMIT @row_limit"
        from google.cloud import bigquery
        config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("row_limit", "INT64", limit),
        ])
        return list(self.client.query(query, job_config=config, location=None).result())

    def assert_profile(self, profile_id: str) -> None:
        self.context.assert_profile(profile_id)

def provisioned_client(context: TenantContext, *, project_id: str):
    from google.cloud import bigquery
    return TenantBigQuery(context, project_id, bigquery.Client(project=project_id))
