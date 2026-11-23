"""Read costs from private Secret Manager and send them to Cloud Run over HTTPS.

No cost values, credentials, plaintext files, or encrypted packets are committed
or printed. Only the importer result (counts and sanitized failures) is logged.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
from urllib.error import HTTPError
from activate_profit_controls import gcloud


class CostImportHTTPError(RuntimeError):
    def __init__(self, step, status):
        self.step = step
        self.status = status
        if status in (401, 403):
            self.category = 'authorization'
        elif status == 404:
            self.category = 'route_not_found'
        elif status == 429:
            self.category = 'rate_limited'
        elif status >= 500:
            self.category = 'service_failure'
        else:
            self.category = 'request_rejected'


def main():
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    try:
        private_schedule = gcloud('secrets', 'versions', 'access', 'latest', '--secret=PPC_VERIFIED_COSTS', '--project', project)
    except RuntimeError as exc:
        if 'NOT_FOUND' in str(exc):
            print('COST_IMPORT_PENDING private PPC_VERIFIED_COSTS secret is not available yet.')
            return
        raise
    # Validate JSON before any transmission; never print parser exceptions.
    json.loads(private_schedule)
    url = gcloud('run', 'services', 'describe', 'campaign-optimizer', '--project', project,
                 '--region', 'us-central1', '--format=value(status.url)')
    token = gcloud('secrets', 'versions', 'access', 'latest', '--secret=DAILY_OPTIMIZER_TOKEN', '--project', project)
    headers = {'X-Daily-Optimizer-Token': token, 'Content-Type': 'application/json'}
    def post(step, route, payload):
        req = urllib.request.Request(url + route, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as response:
                return json.load(response)
        except HTTPError as exc:
            raise CostImportHTTPError(step, exc.code) from None
    public = post('key', '/api/profit/cost-import/key', {'apply_live': True})['public_key']
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / 'public.pem'
        key.write_text(public)
        encrypted = subprocess.run(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', str(key),
            '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256'],
            input=json.dumps(json.loads(private_schedule), separators=(',', ':')).encode(), capture_output=True)
        if encrypted.returncode:
            raise RuntimeError('Private cost encryption failed')
    result = post('import', '/api/profit/cost-import', {'apply_live': True, 'packet': base64.b64encode(encrypted.stdout).decode(),
        'confirm_existing_updates': os.getenv('UPDATE_EXISTING_CONFIRMED_COSTS', 'false') == 'true'})
    print('COST_IMPORT ' + json.dumps(result))
    if result.get('failed'):
        raise RuntimeError('Unverified products remain blocked')


if __name__ == '__main__':
    try:
        main()
    except CostImportHTTPError as exc:
        print(f'COST_IMPORT_FAILED step={exc.step} http_status={exc.status} category={exc.category}')
        raise SystemExit(1)
    except Exception as exc:
        print('COST_IMPORT_FAILED ' + type(exc).__name__)
        raise SystemExit(1)
