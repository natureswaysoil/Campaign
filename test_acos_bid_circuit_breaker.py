from unittest.mock import patch

import server_with_bids


def test_acos_circuit_breaker_never_raises_bad_campaign():
    with patch.object(server_with_bids, "ACOS_MIN_SPEND", 20.0), patch.object(server_with_bids, "ACOS_CEILING", 0.38):
        bid, active, reason = server_with_bids._acos_protected_bid(
            daypart_bid=1.15,
            suggested_bid=1.00,
            metrics={"spend": 80.0, "sales": 40.0},
        )
    assert active is True
    assert reason == "acos_above_ceiling"
    assert bid == 0.5
    assert bid < 1.0


def test_acos_circuit_breaker_caps_zero_sales_more_aggressively():
    bid, active, reason = server_with_bids._acos_protected_bid(
        daypart_bid=0.80,
        suggested_bid=1.00,
        metrics={"spend": 25.0, "sales": 0.0},
    )
    assert active is True
    assert reason == "zero_sales"
    assert bid == 0.35


def test_healthy_campaign_keeps_daypart_bid():
    bid, active, reason = server_with_bids._acos_protected_bid(
        daypart_bid=0.80,
        suggested_bid=0.70,
        metrics={"spend": 40.0, "sales": 160.0},
    )
    assert (bid, active, reason) == (0.80, False, None)

def test_live_retune_fails_closed_without_acos_cache():
    import json
    from unittest.mock import MagicMock

    client = MagicMock()
    client.post.return_value = {"adGroups": []}
    with (
        patch.object(server_with_bids, "AmazonAdsClient", return_value=client),
        patch.object(server_with_bids.server.optimizer_core, "_get_cached_dashboard_summary", return_value=(None, {})),
    ):
        response = server_with_bids.api_retune_existing_bids({"apply_live": True}, None, "secret-token")
    payload = json.loads(response.body)
    assert response.status_code == 503
    assert payload["retryable"] is True
    client.put.assert_not_called()