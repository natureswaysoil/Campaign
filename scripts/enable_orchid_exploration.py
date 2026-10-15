"""Apply the owner's product-only exploration approval, preserving other controls."""
import json
import os
import urllib.request
from activate_profit_controls import gcloud


def main():
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    url = gcloud('run', 'services', 'describe', 'campaign-optimizer', '--project', project,
                 '--region', 'us-central1', '--format=value(status.url)')
    token = gcloud('secrets', 'versions', 'access', 'latest', '--secret=DAILY_OPTIMIZER_TOKEN', '--project', project)
    def request(path, body=None):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body else None,
            headers={'X-Daily-Optimizer-Token': token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.load(response)
    status = request('/api/profit/status')
    policy = status['policy']
    asin = 'B0D7T3TLQP'
    if not any(p['asin'] == asin for p in status['products']):
        raise RuntimeError('Verified product required')
    opt_ins = policy.get('product_exploration', {})
    if asin in opt_ins:
        print('Product exploration already configured; existing settings preserved.')
        return
    if policy['daily_limit'] > 75 or policy['exploration_cvr'] != 0:
        raise RuntimeError('Unexpected account policy; no changes made')
    updated = {**policy, 'product_exploration': {**opt_ins, asin: {'cvr': .01, 'daily_budget': 2.0}}}
    request('/api/profit/policy', {'apply_live': True, 'policy': updated})
    verified = request('/api/profit/status')['policy']
    if verified != updated:
        raise RuntimeError('Policy read-back mismatch')
    print('ORCHID_EXPLORATION_VERIFIED: 1% assumed CVR, $2/day per campaign, account limit preserved. No campaigns activated.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('EXPLORATION_SETUP_FAILED ' + type(exc).__name__)
        raise SystemExit(1)
