"""Private, idempotent import of owner-confirmed per-size costs."""
import base64
import json
import time

# Explicit SKU/ASIN size matches reviewed from the owner's product catalog.
# Mixed-size titles and dry products are intentionally not assigned liquid costs.
MATCHES = {
    'B0D9HT7ND8': ('FR-IJ8R-6LQK', '32oz'),
    'B0DJ1MF2BP': ('VY-T7ZM-760R', '1gal'),
    'B0F8R45FJ2': ('8K-DBU9-JA4K', '1gal'),
    'B0F9B4Q5Z8': ('3L-41WW-8JVG', '2.5gal'),
    'B0F9V3K9L7': ('GA-TZ69-N9XK', '2.5gal'),
    'B0F9V4HQNK': ('IT-ADBS-CXUC', '1gal'),
    'B0F9W6R4K9': ('TY-Z0X8-ENHG', '1gal'),
    'B0FF98QKY7': ('VH-WPH5-BD2P', '1gal'),
    'B0FFZPLCG7': ('XX-XBWB-DF03', '32oz'),
    'B0FFZRM6BD': ('FP-AL1H-WYNQ', '32oz'),
    'B0FG38YYJ5': ('T0-MB9Q-JIKC', '1gal'),
    'B0FGWSKGCY': ('BH-NBDZ-TCRT', '32oz'),
    'B0FMQNT193': ('CC-PTT2-IUMQ', '1gal'),
    'B0GTBZ7N56': ('NWS-FRUIT-32', '32oz'),
    'B0GSCVPS49': ('NWS-SEED-32', '32oz'),
}


def validate_schedule(data, now=None):
    now = time.time() if now is None else now
    import math
    if set(data) != {'verified_at', 'sizes'} or set(data['sizes']) != {'32oz', '1gal', '2.5gal'}:
        raise ValueError('Incomplete confirmed cost schedule')
    at = data['verified_at']
    if not isinstance(at, (int, float)) or not math.isfinite(at) or not 0 <= now - at <= 30 * 86400:
        raise ValueError('Cost confirmation is expired or in the future')
    for pair in data['sizes'].values():
        if not isinstance(pair, list) or len(pair) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in pair):
            raise ValueError('Invalid confirmed costs')
    return data


def seed_products(client, data, *, update_existing=False):
    from automation_store import GCSState
    from decision_sync import refresh_product_market, safe_api_error
    from profit_intelligence import Product, ingest
    saved, existing, updated, failed = 0, 0, 0, []
    # Use the normal ad-write lock so imports cannot race policy/control changes.
    with GCSState('profit-write-guard').locked():
        # Owner confirmation is durable even while Amazon market data is unavailable.
        # It is kept separate from usable economics: no placeholder price/fee records.
        schedule_store = GCSState('profit-confirmed-costs')
        confirmed = schedule_store.read()
        if confirmed.get('verified_at', 0) <= data['verified_at']:
            schedule_store.save(data)
            confirmed = schedule_store.read()
            if confirmed != data:
                raise RuntimeError('Private cost schedule read-back failed')
        else:
            data = confirmed
        current = GCSState('profit-data').read().get('products', {})
        if current and any(asin not in current for asin in MATCHES):
            # Establish the account against an already verified SKU before trying
            # recovery for a catalog SKU that Amazon no longer recognizes.
            anchor = next(iter(current.values()))
            listing = client.listing(anchor['sku'])
            if not any(s.get('asin') == anchor['asin'] for s in listing.get('summaries', [])):
                raise RuntimeError('Verified account anchor no longer matches')
        for asin, (sku, size) in MATCHES.items():
            old = current.get(asin)
            if old and (not update_existing or old['updated_at'] >= data['verified_at']):
                existing += 1
                continue
            if old and old['sku'] != sku:
                failed.append({'asin': asin, 'reason': 'existing_sku_requires_individual_confirmation'})
                continue
            cogs, shipping = data['sizes'][size]
            candidate = {**(old or {}), 'asin': asin, 'sku': sku, 'cogs': cogs, 'shipping': shipping,
                         'updated_at': data['verified_at']}
            try:
                try:
                    market = refresh_product_market(client, candidate, fbm_only=True)
                except Exception as exc:
                    if getattr(getattr(exc, 'response', None), 'status_code', None) != 404:
                        raise
                    candidate['sku'] = client.find_listing_sku(asin)
                    market = refresh_product_market(client, candidate, fbm_only=True)
                product = Product.model_validate(market).model_dump(mode='json')
                ingest(products=[product], source='owner_confirmed:2026-10-14')
                verified = GCSState('profit-data').read().get('products', {}).get(asin)
                if not verified or any(verified[k] != product[k] for k in ('sku', 'cogs', 'shipping', 'updated_at')):
                    raise RuntimeError('Cost read-back failed')
                saved += 1
                updated += int(old is not None)
            except Exception as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                # ASIN identifiers are public; raw errors/responses and costs are not.
                entry = {'asin': asin, 'error': type(exc).__name__, 'http_status': status, **safe_api_error(exc)}
                if str(exc) in ('no_matching_listing', 'ambiguous_listing', 'listing_identity_mismatch'):
                    entry['reason'] = str(exc)
                failed.append(entry)
    return {'cost_schedule_saved': True, 'saved': saved, 'existing_preserved': existing, 'existing_updated': updated, 'failed': failed,
            'seller_id_recovered': getattr(client, 'seller_id_recovered', False) is True, 'catalog_snapshot_unmatched': 10}



def import_public_key():
    from automation_store import GCSState
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    store = GCSState('profit-cost-import-key')
    with store.locked() as state:
        if not state.get('private_pem'):
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            state['private_pem'] = key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            store.save(state)
        key = serialization.load_pem_private_key(state['private_pem'].encode(), password=None)
    return key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def import_encrypted_costs(packet, *, update_existing=False):
    from automation_store import GCSState
    from decision_sync import SellingPartnerClient
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    state = GCSState('profit-cost-import-key').read()
    key = serialization.load_pem_private_key(state['private_pem'].encode(), password=None)
    data = json.loads(key.decrypt(base64.b64decode(packet, validate=True),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)))
    if isinstance(data, dict) and isinstance(data.get('verified_at'), str):
        from datetime import datetime
        stamp = datetime.fromisoformat(data['verified_at'])
        if stamp.tzinfo is None:
            raise ValueError('Confirmation timestamp must include a timezone')
        data['verified_at'] = stamp.timestamp()
    # Replaying a historical setup packet must not renew costs or block deploys.
    at = data.get('verified_at') if isinstance(data, dict) else None
    if isinstance(at, (int, float)) and time.time() - at > 30 * 86400:
        return {'skipped': 'expired_confirmation', 'saved': 0}
    if update_existing:
        return seed_products(SellingPartnerClient(), validate_schedule(data), update_existing=True)
    return seed_products(SellingPartnerClient(), validate_schedule(data))
