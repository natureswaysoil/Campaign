"""Provision the profit tick and report readiness without exposing private values.

Run from the authenticated deployment job. Existing policy/costs are never invented
or overwritten. SP-API Secret Manager values remain server-side references.
"""
import json
import os
import re
import subprocess
import urllib.request
import urllib.error


def gcloud(*args):
    try:
        return subprocess.check_output(['gcloud', *args], text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as exc:
        # Show only the operation and recognized error categories, never arguments,
        # stdout, environment, headers, or a traceback containing credentials.
        details = re.findall(r"PERMISSION_DENIED|NOT_FOUND|INVALID_ARGUMENT|UNAUTHENTICATED|unrecognized arguments|(?:run|secretmanager|cloudscheduler|iam)\.[a-zA-Z]+\.[a-zA-Z]+", exc.stderr or '')
        operation = ' '.join(args[:3])
        raise RuntimeError(operation + ' failed: ' + ', '.join(dict.fromkeys(details))) from None



def initialize_spending_policy(url, token, status):
    """Seed the owner's approved $75 limit once; preserve subsequent dashboard edits."""
    if status.get('policy'):
        return status
    payload = {'apply_live': True, 'policy': {'daily_limit': 75.0}}
    request = urllib.request.Request(url + '/api/profit/policy',
        data=json.dumps(payload).encode(), method='POST',
        headers={'X-Daily-Optimizer-Token': token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if not result.get('success') or result.get('policy', {}).get('daily_limit') != 75.0:
        raise RuntimeError('Approved spending policy was not saved')
    request = urllib.request.Request(url + '/api/profit/status',
        headers={'X-Daily-Optimizer-Token': token})
    with urllib.request.urlopen(request, timeout=60) as response:
        verified = json.load(response)
    if verified.get('policy', {}).get('daily_limit') != 75.0:
        raise RuntimeError('Spending policy read-back did not match')
    print('Owner-approved daily spending limit saved and verified.')
    return verified


SP_API_SECRET_NAMES = {
    'SP_API_CLIENT_ID': 'PPCADSCLIENT',
    'SP_API_CLIENT_SECRET': 'PPCADSSECRET',
    'SP_API_REFRESH_TOKEN': 'PPCADSREFRESHTOKEN',
    'SP_API_SELLER_ID': 'SP_API_SELLER_ID',
    'SP_API_MARKETPLACE_ID': 'SP_API_MARKETPLACE_ID',
}


def align_policy_timezone(url, token, status, timezone):
    """Preserve all owner limits while aligning the accounting day with Ads."""
    policy = status.get('policy')
    if not policy or policy.get('timezone') == timezone:
        return status
    request = urllib.request.Request(url + '/api/profit/policy',
        data=json.dumps({'apply_live': True, 'policy': {**policy, 'timezone': timezone}}).encode(),
        headers={'X-Daily-Optimizer-Token': token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        saved = json.load(response)
    expected = {**policy, 'timezone': timezone}
    if saved.get('policy') != expected:
        raise RuntimeError('Policy timezone update did not preserve saved settings')
    request = urllib.request.Request(url + '/api/profit/status', headers={'X-Daily-Optimizer-Token': token})
    with urllib.request.urlopen(request, timeout=60) as response:
        verified = json.load(response)
    if verified.get('policy') != expected:
        raise RuntimeError('Policy timezone read-back failed')
    print('Policy accounting timezone aligned with Ads; spending limits preserved.')
    return verified


def configure_sp_api_secrets(config, service, scope, project):
    """Bind the owner's SP-API secrets independently of missing seller metadata."""
    existing = {e['name']: e for e in config['spec']['template']['spec']['containers'][0].get('env', [])}
    attach, missing = [], []
    for name, secret in SP_API_SECRET_NAMES.items():
        current = existing.get(name, {})
        ref = current.get('valueFrom', {}).get('secretKeyRef', {})
        if ref.get('name') == secret and ref.get('key') == 'latest':
            continue
        # Preserve already configured seller/marketplace identifiers.
        if name in ('SP_API_SELLER_ID', 'SP_API_MARKETPLACE_ID') and current:
            continue
        try:
            if not gcloud('secrets', 'versions', 'access', 'latest', '--secret=' + secret, '--project', project):
                missing.append(name)
                continue
        except RuntimeError as exc:
            print('SP-API binding unavailable: ' + name + ' (' + secret + '): ' + str(exc))
            missing.append(name)
            continue
        attach.append(name + '=' + secret + ':latest')
    if attach:
        gcloud('run', 'services', 'update', service, *scope, '--update-secrets', ','.join(attach), '--format=value(status.url)')
        verified = json.loads(gcloud('run', 'services', 'describe', service, *scope, '--format=json'))
        bindings = {e['name']: e.get('valueFrom', {}).get('secretKeyRef', {})
                    for e in verified['spec']['template']['spec']['containers'][0].get('env', [])}
        for item in attach:
            name = item.split('=', 1)[0]
            if bindings.get(name) != {'name': SP_API_SECRET_NAMES[name], 'key': 'latest'}:
                raise RuntimeError('SP-API secret binding read-back failed for ' + name)
        print('SP-API secret bindings saved and verified: ' + ', '.join(item.split('=', 1)[0] for item in attach))
    if missing:
        print('SP-API setup still needs: ' + ', '.join(missing))
    else:
        print('SP-API environment bindings present; Amazon authorization remains to be checked by synchronization.')


def main():
    project = os.environ.get('GCP_PROJECT_ID', 'amazon-ppc-bid-optimizer')
    region = 'us-central1'
    service = 'campaign-optimizer'
    scope = ['--project', project, '--region', region]
    config = json.loads(gcloud('run', 'services', 'describe', service, *scope, '--format=json'))
    url = config['status']['url']
    print('Deployment identity: ' + gcloud('auth', 'list', '--filter=status:ACTIVE', '--format=value(account)'))
    print('Runtime identity: ' + config['spec']['template']['spec'].get('serviceAccountName', '(default)'))
    configure_sp_api_secrets(config, service, scope, project)
    token = gcloud('secrets', 'versions', 'access', 'latest', '--secret=DAILY_OPTIMIZER_TOKEN', '--project', project)
    if not token:
        raise RuntimeError('Optimizer token missing')
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print('::add-mask::' + token, flush=True)
    req = urllib.request.Request(url + '/api/profit/status', headers={'X-Daily-Optimizer-Token': token})
    with urllib.request.urlopen(req, timeout=60) as response:
        status = json.load(response)
    status = initialize_spending_policy(url, token, status)
    env = config['spec']['template']['spec']['containers'][0].get('env', [])
    timezone = next((e['value'] for e in env if e['name'] == 'AMAZON_ADS_TIMEZONE' and 'value' in e), 'America/New_York')
    status = align_policy_timezone(url, token, status, timezone)
    print('Saved spending policy: ' + ('present' if status.get('policy') else 'MISSING'))
    print('Saved product economics records: ' + str(len(status.get('products', []))))
    print('Complete spending snapshot: ' + ('present' if status.get('complete_spend_at') else 'awaiting sync'))
    # Reuse the scheduler identity already configured for this application.
    sa = 'campaign-optimizer-scheduler@' + project + '.iam.gserviceaccount.com'
    try:
        gcloud('scheduler', 'jobs', 'describe', 'ppc-profit-tick', '--location', region, '--project', project, '--format=value(state)')
        present = True
    except RuntimeError as exc:
        if 'NOT_FOUND' not in str(exc):
            raise
        present = False
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
    print('Readiness checked. Missing/stale inputs continue to block spending increases.')
    if os.environ.get('VERIFY_PROFIT_SYNC') == 'true':
        from monitor_automation import sync_summary
        request = urllib.request.Request(url + '/api/automation/profit-tick',
            data=b'{"apply_live":true}', headers={'X-Daily-Optimizer-Token': token, 'Content-Type': 'application/json'})
        try:
            response = urllib.request.urlopen(request, timeout=600)
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise RuntimeError('Profit verification HTTP status ' + str(exc.code)) from None
            response = exc
        with response:
            result = json.load(response)
        print('PROFIT_SYNC_CHECK ' + json.dumps(sync_summary(result)))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Subprocess exceptions can include token-bearing arguments; never print them.
        print('Profit setup incomplete: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        raise SystemExit(1)
