from unittest.mock import MagicMock

from optimize_campaigns import AmazonAdsClient


def test_v5_ad_group_bid_recommendation_aggregates_expression_values():
    client = AmazonAdsClient.__new__(AmazonAdsClient)
    client.post = MagicMock(return_value={
        "bidRecommendations": [{
            "bidRecommendationsForTargetingExpressions": [
                {"bidValues": [{"suggestedBid": 0.37}, {"suggestedBid": 0.54}, {"suggestedBid": 0.68}]},
                {"bidValues": [{"suggestedBid": 0.27}, {"suggestedBid": 0.47}, {"suggestedBid": 0.58}]},
                {"bidValues": [{"suggestedBid": 0.25}, {"suggestedBid": 0.40}, {"suggestedBid": 0.53}]},
                {"bidValues": [{"suggestedBid": 0.23}, {"suggestedBid": 0.34}, {"suggestedBid": 0.47}]},
            ]
        }]
    })

    result = client.get_ad_group_bid_recommendation("22", "11")

    assert result == {"low": 0.28, "suggested": 0.4375, "high": 0.565}
    endpoint, payload = client.post.call_args.args[:2]
    assert endpoint == "/sp/targets/bid/recommendations"
    assert payload["recommendationType"] == "BIDS_FOR_EXISTING_AD_GROUP"
    assert payload["campaignId"] == "11"
    assert payload["adGroupId"] == "22"