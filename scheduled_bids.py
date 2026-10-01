"""Persist performance reports across Cloud Run instances before dayparting."""
import time
import json
from datetime import timedelta
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


def retune_tick(payload, retune):
    if not live_requested(payload):
        return JSONResponse({'success': True, 'dry_run': True, 'message': 'No scheduled changes'})
    try:
        store = GCSState('bid-workflow')
        with store.locked() as state:
            now = time.time()
            if now - state.get('ts', 0) >= core._DASH_SUMMARY_TTL or not state.get('per_campaign'):
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
                report = client.get('/reporting/reports/' + state['report_id'], accept='application/json')
                phase = str(report.get('status', '')).upper()
                if phase in {'FAILED', 'FAILURE', 'CANCELLED'}:
                    state.pop('report_id')
                    store.save(state)
                    return JSONResponse({'success': False, 'status': phase}, status_code=502)
                if phase not in {'COMPLETED', 'SUCCESS'}:
                    return JSONResponse({'success': True, 'status': 'metrics_pending'}, status_code=202)
                url = report.get('url') or report.get('location')
                if not url:
                    raise RuntimeError('Performance report missing download URL')
                summary, per = summarize(core.parse_report_json_bytes(client.download_binary(url)))
                state.update(summary=summary, per_campaign=per, ts=time.time())
                state.pop('report_id')
                store.save(state)
            core._dash_summary_cache.update(summary=state['summary'], per_campaign=state['per_campaign'],
                                             ts=state['ts'], refreshing=False)
            response = retune(payload)
            state['last_result'] = json.loads(response.body)
            state['last_run_at'] = time.time()
            store.save(state)
            return response
    except StateBusy as exc:
        return JSONResponse({'success': False, 'message': str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({'success': False, 'message': str(exc)}, status_code=503)
