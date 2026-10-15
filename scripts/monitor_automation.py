"""Read-only monitoring; print counts/status, never credentials or keyword text."""
import json
import os
import re
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def sync_summary(sync):
    """Allowlisted diagnostics only: never emit raw API errors or response bodies."""
    output = {'at': sync.get('at'), 'success': sync.get('success')}
    for name in ('ads', 'retail', 'controls'):
        value = sync.get(name, {})
        if not isinstance(value, dict):
            continue
        entry = {}
        if value.get('error'):
            error = value['error']
            entry['error'] = error if error in ('RuntimeError', 'HTTPError', 'StateBusy', 'ValueError', 'KeyError', 'Timeout', 'ConnectionError') else 'OtherError'
            message = str(value.get('message', ''))
            placement = re.fullmatch(r'Unknown placement: ([A-Za-z ()_.-]{1,64})', message)
            if placement:
                entry['category'] = 'unknown_placement'
                entry['placement_label'] = placement.group(1)
            code = re.search(r'\b([45][0-9]{2}) (?:Client|Server) Error', message)
            if code:
                entry['http_status'] = int(code.group(1))
            for needle, category in (
                ('api.amazon.com/auth/o2/token', 'lwa_token_exchange'),
                ('sellingpartnerapi-na.amazon.com', 'selling_partner_api'),
                ('AMAZON_ADS_TIMEZONE', 'ads_timezone_mismatch'),
                ('SP-API is not configured', 'sp_api_configuration_missing'),
                ('US marketplace and USD', 'marketplace_mismatch'),
                ('profit-data', 'profit_data'),
                ('already running or interrupted', 'state_lock'),
            ):
                if needle in message:
                    entry['category'] = category
                    break
            for zone in ('America/Los_Angeles', 'America/New_York', 'America/Chicago', 'America/Denver', 'UTC'):
                if 'observed: ' + zone == message[-len('observed: ' + zone):]:
                    entry['observed_timezone'] = zone
        for key in ('current', 'campaign', 'keyword', 'placement', 'inventory', 'sales'):
            if value.get(key) in ('requested', 'pending', 'ingested', 'synced', 'current'):
                entry[key] = value[key]
        if isinstance(value.get('success'), bool):
            entry['success'] = value['success']
        output[name] = entry
    return output


def read_states():
    bucket = os.environ.get('AUTOMATION_STATE_BUCKET', 'amazon-ppc-bid-optimizer-ppc-automation-state')
    states = {}
    listing = subprocess.run(['gcloud', 'storage', 'ls', f'gs://{bucket}/*/*-workflow.json'],
                             check=True, capture_output=True, text=True)
    for uri in listing.stdout.splitlines():
        if not uri.startswith('gs://'):
            continue
        kind = uri.rsplit('/', 1)[-1].removesuffix('-workflow.json')
        if kind not in ('bid', 'harvest', 'opportunity', 'sales-growth', 'profit'):
            continue
        result = subprocess.run(['gcloud', 'storage', 'cat', uri], check=True,
                                capture_output=True, text=True)
        states.setdefault(uri.split('/')[-2], {})[kind] = json.loads(result.stdout)
        if kind == 'profit':
            sync_uri = uri.rsplit('/', 1)[0] + '/profit-sync-status.json'
            sync = subprocess.run(['gcloud', 'storage', 'cat', sync_uri],
                                  check=True, capture_output=True, text=True)
            states[uri.split('/')[-2]]['profit_sync'] = sync_summary(json.loads(sync.stdout))
    return states


def summarize(states, now=None):
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, ZoneInfo('America/New_York')).date()
    profiles = []
    for profile, workflows in states.items():
        output = {'profile': profile}
        if 'profit_sync' in workflows:
            output['profit_sync'] = workflows['profit_sync']
        alerts = []
        for kind in ('bid', 'harvest', 'opportunity', 'sales-growth', 'profit'):
            state = workflows.get(kind, {})
            result = state.get('last_result') or {}
            pending = state.get('pending') or {}
            requested = pending.get('requested_at') if kind != 'bid' else state.get('requested_at')
            waiting = bool(pending) if kind != 'bid' else bool(state.get('report_id'))
            age = round((now - requested) / 3600, 2) if waiting and requested else None
            last_run = state.get('last_run_at')
            entry = {'report_pending': waiting, 'pending_hours': age,
                     'report_id': pending.get('report_id') if kind != 'bid' else state.get('report_id'),
                     'last_run_at': last_run, 'last_success': result.get('success')}
            if kind == 'bid':
                entry.update(ad_group_updates=result.get('updates_applied'),
                             keyword_updates=result.get('keyword_dayparting', {}).get('updates_applied'),
                             errors=(result.get('update_errors', 0) + result.get('keyword_dayparting', {}).get('update_errors', 0)))
                if last_run and now - last_run > 7200:
                    alerts.append('No completed bid run in over two hours')
            else:
                entry.update(completed_day=state.get('completed_day'),
                             keywords_created=result.get('keywords_created'),
                             keyword_errors=result.get('keyword_errors'))
                completed = state.get('completed_day')
                if completed and (today - datetime.fromisoformat(completed).date()).days > 1:
                    alerts.append(f'{kind} is more than one day behind')
            if kind == 'profit' and last_run and now - last_run > 7200:
                alerts.append('No completed profit-control run in over two hours')
            if kind == 'opportunity':
                entry['auto_launches'] = len(result.get('auto_launches') or [])
                entry['approvals'] = len(state.get('approvals') or {})
            elif kind == 'sales-growth':
                entry['auto_scales'] = len(result.get('auto_scales') or [])
                entry['recommendations'] = len(state.get('recommendations') or {})
            if age is not None and age > 2:
                alerts.append(f'{kind} report pending over two hours')
            if result.get('success') is False or result.get('error') is True:
                alerts.append(f'{kind} latest apply reported failure')
            if not result:
                entry['verification'] = 'No recorded apply result yet; live changes unverified'
                alerts.append(f'{kind} has no recorded apply result')
            output[kind] = entry
        output['alerts'] = alerts
        profiles.append(output)
    return {'checked_at': datetime.fromtimestamp(now, ZoneInfo('America/New_York')).isoformat(),
            'profiles': profiles, 'alerts': [a for p in profiles for a in p['alerts']]}


if __name__ == '__main__':
    try:
        report = summarize(read_states())
        if not report['profiles']:
            raise RuntimeError('No automation state found')
        print('AUTOMATION_MONITOR ' + json.dumps(report))
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as f:
                f.write('## PPC automation status\n```json\n' + json.dumps(report, indent=2) + '\n```\n')
        raise SystemExit(1 if report['alerts'] else 0)
    except (subprocess.CalledProcessError, ValueError, RuntimeError) as exc:
        # Do not dump subprocess output, which can contain raw private state.
        print('AUTOMATION_MONITOR_ERROR ' + type(exc).__name__ + ': unable to read automation state')
        raise SystemExit(1)
