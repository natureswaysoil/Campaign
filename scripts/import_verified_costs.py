"""Import owner-confirmed costs without placing their values in a public repo.

Only the RSA public key and encrypted packet are public. The private key and
product economics stay in the existing private automation bucket. Imports are
idempotent and never overwrite an existing product or renew verification dates.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def command(*args):
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('Private cost setup command failed: ' + args[0])
    return result.stdout


def private_key(path, uri):
    result = subprocess.run(['gcloud', 'storage', 'cat', uri], capture_output=True)
    if result.returncode == 0:
        path.write_bytes(result.stdout)
    else:
        error = result.stderr.decode(errors='replace').lower()
        if not any(x in error for x in ('no urls matched', 'matched no objects', '404', 'not found')):
            raise RuntimeError('Cannot read private cost import key')
        command('openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(path))
        command('gcloud', 'storage', 'cp', str(path), uri, '--if-generation-match=0')
    path.chmod(0o600)


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


def seed_products(client, data):
    from automation_store import GCSState
    from decision_sync import refresh_product_market
    from profit_intelligence import Product, ingest
    saved, existing, failed = 0, 0, []
    # Use the normal ad-write lock so imports cannot race policy/control changes.
    with GCSState('profit-write-guard').locked():
        current = GCSState('profit-data').read().get('products', {})
        for asin, (sku, size) in MATCHES.items():
            if asin in current:
                existing += 1
                continue
            cogs, shipping = data['sizes'][size]
            candidate = {'asin': asin, 'sku': sku, 'cogs': cogs, 'shipping': shipping,
                         'updated_at': data['verified_at']}
            try:
                product = Product.model_validate(refresh_product_market(client, candidate, fbm_only=True)).model_dump(mode='json')
                ingest(products=[product], source='owner_confirmed:2026-10-14')
                verified = GCSState('profit-data').read().get('products', {}).get(asin)
                if not verified or any(verified[k] != product[k] for k in ('sku', 'cogs', 'shipping', 'updated_at')):
                    raise RuntimeError('Cost read-back failed')
                saved += 1
            except Exception as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                # ASIN identifiers are public; raw errors/responses and costs are not.
                failed.append({'asin': asin, 'error': type(exc).__name__, 'http_status': status})
    print('COST_IMPORT ' + json.dumps({'saved': saved, 'existing_preserved': existing, 'failed': failed, 'unmatched_active_catalog_products': 10}))
    if failed:
        raise RuntimeError('Some product economics could not be verified; unmatched records remain blocked')


def main():
    bucket = os.environ.setdefault('AUTOMATION_STATE_BUCKET', 'amazon-ppc-bid-optimizer-ppc-automation-state')
    uri = 'gs://' + bucket + '/private-setup/cost-import-key.pem'
    packet = ROOT / 'config' / 'verified-costs.b64'
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / 'key.pem'
        private_key(key, uri)
        if not packet.exists():
            public = command('openssl', 'pkey', '-in', str(key), '-pubout').decode()
            print('COST_IMPORT_PUBLIC_KEY_BASE64 ' + base64.b64encode(public.encode()).decode())
            print('Cost import ready; awaiting encrypted owner-confirmed schedule.')
            return
        encrypted = Path(directory) / 'packet.bin'
        encrypted.write_bytes(base64.b64decode(packet.read_text(), validate=True))
        plain = command('openssl', 'pkeyutl', '-decrypt', '-inkey', str(key), '-in', str(encrypted),
                        '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256')
        data = validate_schedule(json.loads(plain))
    from scripts.activate_profit_controls import SP_API_SECRET_NAMES, gcloud
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    for variable, secret in {**SP_API_SECRET_NAMES, 'AMAZON_ADS_PROFILE_ID': 'AMAZON_ADS_PROFILE_ID'}.items():
        os.environ[variable] = gcloud('secrets', 'versions', 'access', 'latest', '--secret=' + secret, '--project', project)
    from decision_sync import SellingPartnerClient
    seed_products(SellingPartnerClient(), data)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('COST_IMPORT_FAILED ' + type(exc).__name__)
        raise SystemExit(1)
