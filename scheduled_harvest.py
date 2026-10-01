"""One bounded scheduler tick: request, poll, or apply a durable daily report."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi.responses import JSONResponse
from automation_store import GCSState, StateBusy
from safety import live_requested
from optimize_campaigns import AmazonAdsClient, _build_search_term_report_body, parse_report_json_bytes


def eastern_day():
    return datetime.now(ZoneInfo('America/New_York')).date()


def harvest_tick(payload, apply_rows):
    if not live_requested(payload):
        return JSONResponse({'success': True, 'dry_run': True,
                             'message': 'Scheduled report workflow preview; no state or ads changed'})
    try:
        store = GCSState('harvest-workflow')
        with store.locked() as state:
            today = eastern_day()
            if state.get('completed_day') == today.isoformat():
                return JSONResponse({'success': True, 'status': 'already_completed'})
            client = AmazonAdsClient()
            pending = state.get('pending')
            if not pending:
                body = _build_search_term_report_body(14)
                body['startDate'] = (today - timedelta(days=14)).isoformat()
                body['endDate'] = (today - timedelta(days=1)).isoformat()
                report_id = client.request_report(body)
                state['pending'] = {'report_id': report_id, 'day': today.isoformat(),
                                    'start': body['startDate'], 'end': body['endDate']}
                store.save(state)
                return JSONResponse({'success': True, 'status': 'requested', 'report_id': report_id}, status_code=202)
            status = client.get('/reporting/reports/' + pending['report_id'], accept='application/json')
            phase = str(status.get('status', '')).upper()
            if phase in {'FAILURE', 'FAILED', 'CANCELLED'}:
                state['last_failure'] = {'report_id': pending['report_id'], 'status': phase}
                state.pop('pending', None)
                store.save(state)
                return JSONResponse({'success': False, 'status': phase}, status_code=502)
            if phase not in {'SUCCESS', 'COMPLETED'}:
                return JSONResponse({'success': True, 'status': phase or 'pending'}, status_code=202)
            url = status.get('url') or status.get('location')
            if not url:
                raise RuntimeError('Completed report has no download URL')
            rows = parse_report_json_bytes(client.download_binary(url))
            response = apply_rows({'apply_live': True, 'max_products': 100,
                                   'max_terms_per_product': 10}, client, rows,
                                  pending['report_id'], pending['start'], pending['end'])
            result = json.loads(response.body)
            state['last_result'] = result
            # On partial failure keep the report. Next tick re-lists existing
            # keywords, skips acknowledged inserts, and retries missing terms.
            if response.status_code < 400 and result.get('success'):
                state['completed_day'] = pending['day']
                state.pop('pending', None)
            store.save(state)
            return response
    except StateBusy as exc:
        return JSONResponse({'success': False, 'message': str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({'success': False, 'message': str(exc)}, status_code=503)
