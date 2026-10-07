"""Read configured bids without inventing campaign-level bids or estimates."""
import math
from concurrent.futures import ThreadPoolExecutor


def _positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def enrich_campaign_bids(client, campaigns):
    try:
        groups = client.list_ad_groups()
    except Exception:
        for campaign in campaigns:
            campaign['bidDataStatus'] = 'unavailable'
        return

    def read(campaign):
        cid = str(campaign.get('campaignId') or '')
        active = [g for g in groups if str(g.get('campaignId')) == cid and str(g.get('state', '')).upper() == 'ENABLED']
        gids = {str(g.get('adGroupId')) for g in active}
        values = [_positive(g.get('defaultBid')) for g in active]
        complete = True
        # Explicit keyword and target bids can override ad-group defaults.
        for loader in (client.list_keywords, client.list_targets):
            try:
                values.extend(_positive(row.get('bid')) for row in loader(cid)
                              if str(row.get('adGroupId')) in gids and str(row.get('state', '')).upper() == 'ENABLED')
            except Exception:
                complete = False
        values = [v for v in values if v is not None]
        campaign['bidDataStatus'] = 'complete' if complete else 'partial'
        campaign['configuredBidLow'] = min(values) if values else None
        campaign['configuredBidHigh'] = max(values) if values else None
        campaign['currentAppliedBid'] = None  # A campaign need not have a single bid.

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(read, campaigns))
