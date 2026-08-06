import json
from unittest.mock import MagicMock, patch

from fastapi.responses import JSONResponse

import extended_server
import final_server


def test_final_server_registers_complete_production_routes():
    paths = {(route.path, method) for route in final_server.app.routes for method in getattr(route, "methods", set())}
    assert ("/api/create-campaign-from-product", "POST") in paths
    assert ("/api/harvest-discovery-winners", "POST") in paths
    assert ("/api/harvest-all-discovery", "POST") in paths
    assert ("/api/retune-existing-bids", "POST") in paths


def test_launch_preview_never_calls_live_launcher():
    product = {"title": "Example", "sku": "sku-1", "asin": "B001"}
    client = MagicMock()
    client.list_campaigns.return_value = []
    with (
        patch.object(extended_server, "_product_from_key", return_value=(product, {})),
        patch.object(extended_server, "AmazonAdsClient", return_value=client),
        patch.object(extended_server.base, "api_create_recommended_campaigns") as live_launcher,
    ):
        response = extended_server.api_create_campaign_with_duplicate_protection(
            {"product_id": "p1", "apply_live": False}, None, "secret-token"
        )
    payload = json.loads(response.body)
    assert payload["dry_run"] is True
    assert payload["apply_live"] is False
    live_launcher.assert_not_called()


def test_batch_harvest_uses_one_shared_report_and_stays_dry_run():
    client = MagicMock()
    with (
        patch.object(final_server, "AmazonAdsClient", return_value=client),
        patch.object(final_server, "load_products", return_value=[]),
        patch.object(
            extended_server,
            "_search_term_rows",
            return_value=([{"campaignId": "1", "searchTerm": "soil"}], "report-1", "2026-07-20", "2026-08-03"),
        ) as report,
    ):
        response = final_server.api_harvest_all_discovery({"apply_live": False}, None, "secret-token")
    payload = json.loads(response.body)
    assert payload["apply_live"] is False
    assert payload["report_id"] == "report-1"
    assert payload["report_rows"] == 1
    assert payload["keywords_created"] == 0
    report.assert_called_once_with(client, 14)

def test_dashboard_refresh_returns_immediately_while_report_runs():
    cache = {"summary": None, "per_campaign": {}, "ts": 0.0, "refreshing": True}
    with (
        patch.object(extended_server.base.optimizer_core, "_dash_summary_cache", cache),
        patch.object(
            extended_server.base.optimizer_core,
            "_get_cached_dashboard_summary",
            return_value=(None, {}),
        ),
    ):
        response = final_server.api_refresh_dashboard_cache(None, "secret-token")
    payload = json.loads(response.body)
    assert response.status_code == 202
    assert payload["refreshing"] is True
    assert payload["cache_ready"] is False
