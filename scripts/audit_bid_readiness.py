"""Read-only list of campaigns and advertised ASINs needing verified economics."""
import json
from optimize_campaigns import AmazonAdsClient
from automation_store import GCSState


def audit(client, products):
    result = []
    for campaign in client.list_campaigns():
        cid = str(campaign['campaignId'])
        for ad in client.list_product_ads(cid):
            if ad.get('state') == 'ARCHIVED':
                continue
            asin = str(ad.get('asin') or '')
            sku = str(ad.get('sku') or '')
            product = products.get(asin)
            if not product and sku:
                matches = [p for p in products.values() if p.get('sku') == sku]
                product = matches[0] if len(matches) == 1 else None
            result.append({'campaign_id': cid, 'asin': asin, 'sku': sku,
                           'verified_mapping': bool(product)})
    return result


if __name__ == '__main__':
    client = AmazonAdsClient()
    products = GCSState('profit-data').read().get('products', {})
    print(json.dumps({'read_only': True, 'mappings': audit(client, products)}, indent=2))
