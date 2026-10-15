"""Send only encrypted costs to the authenticated Cloud Run importer."""
import base64
import json
import os
from pathlib import Path
import urllib.request
from activate_profit_controls import gcloud


def main():
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    url = gcloud('run', 'services', 'describe', 'campaign-optimizer', '--project', project,
                 '--region', 'us-central1', '--format=value(status.url)')
    token = gcloud('secrets', 'versions', 'access', 'latest', '--secret=DAILY_OPTIMIZER_TOKEN', '--project', project)
    headers = {'X-Daily-Optimizer-Token': token, 'Content-Type': 'application/json'}
    packet = Path(__file__).resolve().parent.parent / 'config' / 'verified-costs.b64'
    if packet.exists():
        route, payload = '/api/profit/cost-import', {'apply_live': True, 'packet': packet.read_text()}
    else:
        route, payload = '/api/profit/cost-import/key', {'apply_live': True}
    req = urllib.request.Request(url + route, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=900) as response:
        result = json.load(response)
    if 'public_key' in result:
        print('COST_IMPORT_PUBLIC_KEY_BASE64 ' + base64.b64encode(result['public_key'].encode()).decode())
        print('Cost import ready; awaiting encrypted owner-confirmed schedule.')
    else:
        print('COST_IMPORT ' + json.dumps(result))
        if result.get('failed'):
            raise RuntimeError('Unverified products remain blocked')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('COST_IMPORT_FAILED ' + type(exc).__name__)
        raise SystemExit(1)
