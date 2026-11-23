"""Persist performance reports across Cloud Run instances before dayparting."""
import time
import json
from datetime import timedelta
from requests import HTTPError
from fastapi.responses import JSONResponse
from automation_store import GCSState, StateBusy
from scheduled_harvest import eastern_day
from safety import live_requested
import optimize_campaigns as core


def summarize(rows):
    per = {}
    total = {'spend': 0., 'sales': 0., 'clicks': 0, 'orders': 0}
    for row in rows:
        cid = str(row.get('campaignId') or '')
        if not cid:
            continue
        cost, sales = float(row.get('cost') or 0), float(row.get('sales14d') or 0)
        metrics = {'spend': cost, 'sales': sales, 'clicks': int(row.get('clicks') or 0),
                   'orders': int(row.get('purchases14d') or 0),
                   'impressions': int(row.get('impressions') or 0),
                   'acos': cost / sales if sales else None}
        per[cid] = metrics
        for key in total:
            total[key] += metrics[key]
    total['acos'] = total['spend'] / total['sales'] if total['sales'] else None
    return total, per


REPORT_TIMEOUT_SECONDS = 7200


def _record_outcome(store, state, result):
    state.update(last_result=result, last_run_at=time.time())
    store.save(state)


def _report_failure(store, state, phase, category, *, upstream_status=None,
                    replace_report=False, retryable=True):
    if replace_report:
        state.pop('report_id', None)
        state.pop('requested_at', None)
    result = {'success': False, 'status': 'execution_failure' if phase == 'apply' else 'report_failure',
              'diagnostic': {'phase': phase, 'category': category, 'retryable': retryable}}
    if upstream_status is not None:
        result['diagnostic']['http_status'] = upstream_status
    _record_outcome(store, state, result)
    return JSONResponse(result, status_code=503 if retryable else 502)


def retune_tick(payload, retune):
    if not live_requested(payload):
        return JSONResponse({'success': True, 'dry_run': True, 'message': 'No scheduled changes'})
    try:
        store = GCSState('bid-workflow')
        with store.locked() as state:
            phase = 'request'
            try:
                now = time.time()
                if (state.get('report_id') or now - state.get('ts', 0) >= core._DASH_SUMMARY_TTL
                        or not state.get('per_campaign')):
                    client = core.AmazonAdsClient()
                    if not state.get('report_id'):
                        body = core._build_dashboard_report_body()
                        today = eastern_day()
                        body['startDate'] = (today - timedelta(days=14)).isoformat()
                        body['endDate'] = (today - timedelta(days=1)).isoformat()
                        state['report_id'] = client.request_report(body)
                        state['requested_at'] = time.time()
                        store.save(state)
                        return JSONResponse({'success': True, 'status': 'metrics_requested'}, status_code=202)
                    phase = 'poll'
                    report = client.get('/reporting/reports/' + state['report_id'], accept='application/json')
                    status = str(report.get('status', '')).upper()
                    if status in {'FAILED', 'FAILURE', 'CANCELLED'}:
                        return _report_failure(store, state, phase, 'terminal_report', replace_report=True)
                    if status not in {'COMPLETED', 'SUCCESS'}:
                        if now - (state.get('requested_at') or 0) >= REPORT_TIMEOUT_SECONDS:
                            return _report_failure(store, state, phase, 'pending_timeout', replace_report=True)
                        return JSONResponse({'success': True, 'status': 'metrics_pending'}, status_code=202)
                    phase = 'download'
                    url = report.get('url') or report.get('location')
                    if not url:
                        return _report_failure(store, state, phase, 'missing_download_url', replace_report=True)
                    content = client.download_binary(url)
                    phase = 'parse'
                    summary, per = summarize(core.parse_report_json_bytes(content))
                    state.update(summary=summary, per_campaign=per, ts=time.time())
                    state.pop('report_id')
                    state.pop('requested_at', None)
                    store.save(state)
                core._dash_summary_cache.update(summary=state['summary'], per_campaign=state['per_campaign'],
                                                 ts=state['ts'], refreshing=False)
                phase = 'apply'
                response = retune(payload)
                result = json.loads(response.body)
                if response.status_code >= 400 or result.get('error') is True or result.get('success') is False:
                    counts = result.get('bid_outcomes') or {}
                    category = ('unconfirmed_bid_update' if counts.get('unconfirmed') else
                                'rejected_bid_update' if counts.get('rejected') else
                                'stale_metrics' if result.get('retryable') is True else 'retune_failed')
                    result = {key: result[key] for key in ('bid_outcomes', 'updates_applied',
                              'update_errors', 'keyword_dayparting') if key in result}
                    if 'keyword_dayparting' in result:
                        result['keyword_dayparting'] = {key: value for key, value in result['keyword_dayparting'].items()
                                                        if key in ('updates_applied', 'update_errors')}
                    result.update(success=False, status='execution_failure', diagnostic={
                        'phase': phase, 'category': category, 'retryable': category == 'stale_metrics',
                        'http_status': response.status_code})
                    response = JSONResponse(result, status_code=response.status_code)
                _record_outcome(store, state, result)
                return response
            except HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else None
                replace = ((phase == 'poll' and code in {404, 410}) or
                           (phase == 'download' and code in {401, 403, 404, 410}))
                return _report_failure(store, state, phase, 'report_unavailable' if replace else 'http_error',
                                       upstream_status=code, replace_report=replace,
                                       retryable=phase != 'apply' and (replace or code is None or code in {408, 429} or code >= 500))
            except StateBusy:
                raise
            except Exception:
                return _report_failure(store, state, phase, 'invalid_report' if phase == 'parse' else 'unexpected_error',
                                       replace_report=phase == 'parse', retryable=phase != 'apply')
    except StateBusy:
        return JSONResponse({'success': False, 'status': 'state_busy'}, status_code=409)
    except Exception:
        return JSONResponse({'success': False, 'status': 'state_unavailable'}, status_code=503)
