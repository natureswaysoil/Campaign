"""Read an authenticated launch diagnostic; never log costs or credentials."""
import json
import os
import sys
import urllib.request
from activate_profit_controls import gcloud


def main():
    campaign_id = sys.argv[1]
    if not campaign_id.isdigit():
        raise ValueError('Numeric campaign ID required')
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    url = gcloud('run', 'services', 'describe', 'campaign-optimizer', '--project', project,
                 '--region', 'us-central1', '--format=value(status.url)')
    token = gcloud('secrets', 'versions', 'access', 'latest', '--secret=DAILY_OPTIMIZER_TOKEN', '--project', project)
    req = urllib.request.Request(url + '/api/profit/launch-diagnostic/' + campaign_id,
                                headers={'X-Daily-Optimizer-Token': token})
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.load(response)
    print('LAUNCH_DIAGNOSTIC ' + json.dumps({key: result.get(key) for key in
        ('campaign_id', 'state', 'product_ads', 'catalog_candidates')}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('LAUNCH_DIAGNOSTIC_FAILED ' + type(exc).__name__)
        raise SystemExit(1)
