"""Read-only Ads/SP-API connectors and resumable asynchronous report ingestion.

No customer names, addresses, or order-level PII are requested or persisted.
SP-API credentials are separate from Ads credentials. Errors never become zeros.
"""
from __future__ import annotations
import gzip
import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from automation_store import GCSState
from profit_intelligence import ET, ingest


class SellingPartnerClient:
    def __init__(self):
        names = ('SP_API_CLIENT_ID', 'SP_API_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN', 'SP_API_SELLER_ID', 'SP_API_MARKETPLACE_ID')
        values = {n: os.getenv(n, '').strip() for n in names}
        if not all(values.values()):
            raise RuntimeError('SP-API is not configured: separate credentials, seller ID and marketplace ID required')
        self.seller, self.marketplace = values['SP_API_SELLER_ID'], values['SP_API_MARKETPLACE_ID']
        if self.marketplace != 'ATVPDKIKX0DER':
            raise RuntimeError('Profit controls currently require the US marketplace and USD')
        self.session = requests.Session()
        response = self.session.post('https://api.amazon.com/auth/o2/token', data={
            'grant_type': 'refresh_token', 'client_id': values['SP_API_CLIENT_ID'],
            'client_secret': values['SP_API_CLIENT_SECRET'], 'refresh_token': values['SP_API_REFRESH_TOKEN']}, timeout=30)
        response.raise_for_status()
        self.token = response.json()['access_token']

    def request(self, method, path, params=None, body=None):
        for attempt in range(5):
            response = self.session.request(method, 'https://sellingpartnerapi-na.amazon.com' + path,
                headers={'x-amz-access-token': self.token, 'Content-Type': 'application/json'},
                params=params, json=body, timeout=45)
            if response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
                return response.json()
            if attempt < 4:
                try:
                    delay = max(2 ** attempt, min(30, float(response.headers.get('Retry-After', 0))))
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(delay)
        response.raise_for_status()

    def listing(self, sku):
        return self.request('GET', '/listings/2021-08-01/items/' + quote(self.seller, safe='') + '/' + quote(sku, safe=''),
            params={'marketplaceIds': self.marketplace, 'includedData': 'summaries,offers,fulfillmentAvailability'})

    def fees(self, asin, price, fba):
        result = self.request('POST', '/products/fees/v0/items/' + quote(asin, safe='') + '/feesEstimate', body={
            'FeesEstimateRequest': {'MarketplaceId': self.marketplace, 'IsAmazonFulfilled': fba,
            'Identifier': asin, 'PriceToEstimateFees': {'ListingPrice': {'CurrencyCode': 'USD', 'Amount': price}}}})
        item = result.get('payload', {}).get('FeesEstimateResult', {})
        if item.get('Status') != 'Success':
            raise RuntimeError('Amazon fee estimate not available')
        total = item['FeesEstimate']['TotalFeesEstimate']
        if total['CurrencyCode'] != 'USD':
            raise RuntimeError('Fee currency mismatch')
        return float(total['Amount'])

    def fba_units(self, sku):
        result = self.request('GET', '/fba/inventory/v1/summaries', params={
            'granularityType': 'Marketplace', 'granularityId': self.marketplace,
            'marketplaceIds': self.marketplace, 'details': 'true', 'sellerSkus': sku})
        rows = [r for r in result.get('payload', {}).get('inventorySummaries', []) if r.get('sellerSku') == sku]
        if len(rows) != 1:
            raise RuntimeError('FBA availability missing or ambiguous')
        return int(rows[0]['inventoryDetails']['fulfillableQuantity'])

    def request_sales(self, day):
        start = datetime.fromisoformat(day).replace(tzinfo=ET)
        # Amazon reports expect ISO timestamps; local calendar boundaries include DST correctly.
        return self.request('POST', '/reports/2021-06-30/reports', body={
            'reportType': 'GET_SALES_AND_TRAFFIC_REPORT', 'marketplaceIds': [self.marketplace],
            'dataStartTime': start.isoformat(), 'dataEndTime': (start + timedelta(days=1) - timedelta(seconds=1)).isoformat(),
            'reportOptions': {'dateGranularity': 'DAY', 'asinGranularity': 'CHILD'}})['reportId']

    def poll_sales(self, report_id):
        result = self.request('GET', '/reports/2021-06-30/reports/' + quote(report_id, safe=''))
        status = result['processingStatus']
        if status in ('FATAL', 'CANCELLED'):
            raise RuntimeError('SP-API sales report ' + status)
        if status != 'DONE':
            return None
        document = self.request('GET', '/reports/2021-06-30/documents/' + quote(result['reportDocumentId'], safe=''))
        response = requests.get(document['url'], timeout=90)
        response.raise_for_status()
        content = gzip.decompress(response.content) if document.get('compressionAlgorithm') == 'GZIP' else response.content
        return json.loads(content)


def ads_report_body(kind, today):
    current = kind == 'current'
    columns = ['date', 'campaignId', 'impressions', 'clicks', 'cost', 'sales14d', 'purchases14d']
    if kind == 'keyword':
        report_type, group = 'spTargeting', ['targeting']
        columns += ['keywordId', 'adGroupId', 'keywordType']
    else:
        report_type, group = 'spCampaigns', ['campaignPlacement'] if kind == 'placement' else ['campaign']
        if kind == 'placement':
            columns += ['placementClassification']
    return {'name': 'profit-' + kind, 'startDate': (today if current else today - timedelta(days=30)).isoformat(),
            'endDate': (today if current else today - timedelta(days=1)).isoformat(),
            'configuration': {'adProduct': 'SPONSORED_PRODUCTS', 'groupBy': group, 'columns': columns,
                'reportTypeId': report_type, 'timeUnit': 'DAILY', 'format': 'GZIP_JSON'}}


PLACEMENTS = {'Top of Search on-Amazon': 'PLACEMENT_TOP', 'Other on-Amazon': 'PLACEMENT_REST_OF_SEARCH',
              'Detail Page on-Amazon': 'PLACEMENT_PRODUCT_PAGE',
              'PLACEMENT_TOP': 'PLACEMENT_TOP', 'PLACEMENT_REST_OF_SEARCH': 'PLACEMENT_REST_OF_SEARCH',
              'PLACEMENT_PRODUCT_PAGE': 'PLACEMENT_PRODUCT_PAGE'}


def normalize_ads(rows, kind, observed):
    normalized = []
    for r in rows:
        item = {'grain': 'campaign' if kind in ('current', 'campaign') else kind, 'date': r['date'],
                'campaign_id': str(r['campaignId']), 'clicks': int(r.get('clicks') or 0),
                'orders': int(r.get('purchases14d') or 0), 'impressions': int(r.get('impressions') or 0),
                'spend': float(r.get('cost') or 0), 'sales': float(r.get('sales14d') or 0), 'observed_at': observed}
        if kind == 'keyword':
            if r.get('keywordId') is None:
                continue  # Never pretend campaign totals are keyword performance.
            item['target_id' if r.get('keywordType') in ('TARGETING_EXPRESSION', 'TARGETING_EXPRESSION_PREDEFINED') else 'keyword_id'] = str(r['keywordId'])
        if kind == 'placement':
            value = PLACEMENTS.get(r.get('placementClassification'))
            if not value:
                raise ValueError('Unknown placement: ' + str(r.get('placementClassification')))
            item['placement'] = value
        normalized.append(item)
    return normalized


def sync_ads(client):
    today = datetime.now(ET).date()
    store = GCSState('profit-ads-sync')
    outcomes = {}
    with store.locked() as state:
        # Verify currency/timezone rather than combining mismatched clocks or currencies.
        profiles = client.get('/v2/profiles')
        profile = next((p for p in profiles if str(p.get('profileId')) == str(client.profile_id)), None)
        if not profile or profile.get('currencyCode') != 'USD' or profile.get('timezone') != ET.key:
            raise RuntimeError('Set AMAZON_ADS_TIMEZONE to the USD Ads profile timezone; observed: ' + str((profile or {}).get('timezone')))
        for kind in ('current', 'campaign', 'keyword', 'placement'):
            job = state.setdefault(kind, {})
            if not job.get('report_id'):
                if time.time() - job.get('completed_at', 0) < (900 if kind == 'current' else 21600):
                    continue
                body = ads_report_body(kind, today)
                job.update(report_id=client.request_report(body), start=body['startDate'], end=body['endDate'], requested_at=time.time())
                store.save(state)
                outcomes[kind] = 'requested'
                continue
            result = client.get('/reporting/reports/' + job['report_id'], accept='application/json')
            if result.get('status') in ('FAILED', 'FAILURE', 'CANCELLED'):
                job.pop('report_id')
                store.save(state)
                raise RuntimeError(kind + ' report failed; next tick will request again')
            if result.get('status') not in ('COMPLETED', 'SUCCESS'):
                if time.time() - job['requested_at'] > 7200:
                    job.pop('report_id', None)
                    store.save(state)
                    raise RuntimeError(kind + ' report pending over two hours; replacement scheduled')
                outcomes[kind] = 'pending'
                continue
            from optimize_campaigns import parse_report_json_bytes
            raw = parse_report_json_bytes(client.download_binary(result['url']))
            # Freshness measured from request, not download; an old pending report is not real-time.
            observed = job['requested_at']
            rows = normalize_ads(raw, kind, observed)
            grain = 'campaign' if kind in ('current', 'campaign') else kind
            ingest(metrics=rows, source='amazon_ads:' + kind,
                   complete_spend_at=observed if kind == 'current' and job['end'] == today.isoformat() else None,
                   replace_window=(grain, job['start'], job['end'], observed))
            job.pop('report_id')
            job['completed_at'] = time.time()
            store.save(state)
            outcomes[kind] = 'ingested'
    return outcomes


def sync_retail(client):
    store = GCSState('profit-retail-sync')
    outcomes = {}
    with store.locked() as state:
        data = GCSState('profit-data').read()
        products = list(data.get('products', {}).values())
        # Five SKUs per tick bounds API load. Failures leave the cursor at the failed SKU.
        cursor = state.get('cursor', 0) % max(1, len(products))
        for product in products[cursor:cursor + 5]:
            listing = client.listing(product['sku'])
            summaries = listing.get('summaries', [])
            if not any(r.get('asin') == product['asin'] for r in summaries):
                raise RuntimeError('Listing SKU/ASIN mismatch')
            offers = [r for r in listing.get('offers', []) if r.get('marketplaceId') == client.marketplace and r.get('offerType') == 'B2C']
            if len(offers) != 1 or offers[0].get('price', {}).get('currencyCode') != 'USD':
                raise RuntimeError('Unambiguous USD offer required')
            available = listing.get('fulfillmentAvailability', [])
            fbm = [r for r in available if r.get('fulfillmentChannelCode') == 'DEFAULT']
            fba = not fbm
            units = client.fba_units(product['sku']) if fba else int(fbm[0]['quantity'])
            price = float(offers[0]['price']['amount'])
            # Local COGS/shipping verification time is not renewed by an Amazon fee lookup.
            updated = {**product, 'price': price, 'fees': client.fees(product['asin'], price, fba),
                       'fee_source': 'estimate', 'market_at': time.time(), 'inventory': units, 'inventory_at': time.time()}
            ingest(products=[updated], source='sp_api:listing_fees_inventory')
            cursor += 1
            state['cursor'] = cursor
            store.save(state)
        pending = state.get('pending')
        if not pending:
            today = datetime.now(ET).date()
            refreshed = state.setdefault('refreshed', {})
            days = [(today - timedelta(days=i)).isoformat() for i in range(2, 16)]
            wanted = min(days, key=lambda d: (refreshed.get(d, 0), -int(d.replace('-', ''))))
            if time.time() - refreshed.get(wanted, 0) < 21600:
                return {'inventory': 'synced', 'sales': 'current'}
            state['pending'] = {'day': wanted, 'report_id': client.request_sales(wanted), 'at': time.time()}
            store.save(state)
            return {'inventory': 'synced', 'sales': 'requested'}
        try:
            report = client.poll_sales(pending['report_id'])
        except RuntimeError as exc:
            if 'FATAL' in str(exc) or 'CANCELLED' in str(exc):
                state.pop('pending', None)
                store.save(state)
            raise
        if report is None:
            if time.time() - pending['at'] > 7200:
                state.pop('pending', None)
                store.save(state)
                raise RuntimeError('Retail sales report pending over two hours; replacement scheduled')
            return {'inventory': 'synced', 'sales': 'pending'}
        rows = []
        for r in report.get('salesAndTrafficByAsin', []):
            s = r['salesByAsin']
            if s['orderedProductSales']['currencyCode'] != 'USD':
                raise RuntimeError('Retail sales currency mismatch')
            rows.append({'grain': 'retail', 'date': pending['day'], 'asin': r['childAsin'],
                         'units': s['unitsOrdered'], 'orders': s['totalOrderItems'],
                         'sales': s['orderedProductSales']['amount'], 'observed_at': time.time()})
        ingest(metrics=rows, source='sp_api:sales_traffic', replace_window=('retail', pending['day'], pending['day'], time.time()))
        state.setdefault('refreshed', {})[pending['day']] = time.time()
        state.pop('pending')
        store.save(state)
        # Calculate demand using complete calendar days, including zero-sale days.
        data = GCSState('profit-data').read()
        recent = [(datetime.now(ET).date() - timedelta(days=i)).isoformat() for i in range(2, 9)]
        if all(d in state['refreshed'] and time.time() - state['refreshed'][d] < 172800 for d in recent):
            updates = []
            for p in data.get('products', {}).values():
                units = sum(r['units'] for r in data.get('metrics', []) if r['grain'] == 'retail' and r['asin'] == p['asin'] and r['date'] in recent)
                updates.append({**p, 'daily_units': units / 7, 'velocity_at': time.time()})
            ingest(products=updates, source='sp_api:sales_velocity')
        return {'inventory': 'synced', 'sales': 'ingested'}
