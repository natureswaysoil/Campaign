"""Provision the profit tick and report readiness without exposing private values.

Run from the authenticated deployment job. Existing policy/costs are never invented
or overwritten. SP-API Secret Manager values remain server-side references.
"""
import json
import os
import re
import subprocess
import urllib.request


def gcloud(*args):
    try:
        return subprocess.check_output(['gcloud', *args], text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as exc:
        # Show only the operation and recognized error categories, never arguments,
        # stdout, environment, headers, or a traceback containing credentials.
        details = re.findall(r"PERMISSION_DENIED|NOT_FOUND|INVALID_ARGUMENT|UNAUTHENTICATED|unrecognized arguments|(?:run|secretmanager|cloudscheduler|iam)\.[a-zA-Z]+\.[a-zA-Z]+", exc.stderr or '')
        operation = ' '.join(args[:3])
        raise RuntimeError(operation + ' failed: ' + ', '.join(dict.fromkeys(details))) from None


def main():
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    region = 'us-central1'
    service = 'campaign-optimizer'
    scope = ['--project', project, '--region', region]
    config = json.loads(gcloud('run', 'services', 'describe', service, *scope, '--format=json'))
    url = config['status']['url']
    existing = {e['name'] for e in config['spec']['template']['spec']['containers'][0].get('env', [])}
    required = ['SP_API_CLIENT_ID', 'SP_API_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN',
                'SP_API_SELLER_ID', 'SP_API_MARKETPLACE_ID']
    available = {s['name'].split('/')[-1] for s in json.loads(gcloud('secrets', 'list', '--project', project, '--format=json'))}
    missing = [name for name in required if name not in existing and name not in available]
    if missing:
        print('SP-API setup missing: ' + ', '.join(missing))
    else:
        attach = [name + '=' + name + ':latest' for name in required if name not in existing]
        if attach:
            gcloud('run', 'services', 'update', service, *scope, '--update-secrets', ','.join(attach), '--format=value(status.url)')
        print('SP-API environment bindings present; authorization will be checked by synchronization.')
    token = gcloud('secrets', 'versions', 'access', 'latest', '--secret=DAILY_OPTIMIZER_TOKEN', '--project', project)
    if not token:
        raise RuntimeError('Optimizer token missing')
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print('::add-mask::' + token, flush=True)
    # Reuse the scheduler identity already configured for this application.
    sa = 'campaign-optimizer-scheduler@' + project + '.iam.gserviceaccount.com'
    jobs = json.loads(gcloud('scheduler', 'jobs', 'list', '--location', region, '--project', project, '--format=json'))
    present = any(j['name'].endswith('/ppc-profit-tick') for j in jobs)
    gcloud('scheduler', 'jobs', 'update' if present else 'create', 'http', 'ppc-profit-tick',
           '--location', region, '--project', project, '--schedule=*/15 * * * *', '--time-zone=America/New_York',
           '--uri=' + url + '/api/automation/profit-tick', '--http-method=POST',
           ('--update-headers=' if present else '--headers=') + 'Content-Type=application/json,X-Daily-Optimizer-Token=' + token,
           '--message-body={"apply_live":true}', '--oidc-service-account-email=' + sa,
           '--oidc-token-audience=' + url, '--attempt-deadline=1800s', '--max-retry-attempts=3',
           '--min-backoff=60s', '--max-backoff=300s', '--format=value(name)')
    job = json.loads(gcloud('scheduler', 'jobs', 'describe', 'ppc-profit-tick', '--location', region, '--project', project, '--format=json'))
    if job['state'] == 'PAUSED':
        gcloud('scheduler', 'jobs', 'resume', 'ppc-profit-tick', '--location', region, '--project', project, '--format=value(name)')
    print('Profit scheduler configured every 15 minutes.')
    req = urllib.request.Request(url + '/api/profit/status', headers={'X-Daily-Optimizer-Token': token})
    with urllib.request.urlopen(req, timeout=60) as response:
        status = json.load(response)
    print('Saved spending policy: ' + ('present' if status.get('policy') else 'MISSING'))
    print('Saved product economics records: ' + str(len(status.get('products', []))))
    print('Complete spending snapshot: ' + ('present' if status.get('complete_spend_at') else 'awaiting sync'))
    print('Readiness checked. Missing/stale inputs continue to block spending increases.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Subprocess exceptions can include token-bearing arguments; never print them.
        print('Profit setup incomplete: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        raise SystemExit(1)
