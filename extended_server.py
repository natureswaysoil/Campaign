"""Extended Cloud Run entrypoint.

This wraps server.py and overrides only the launch route so duplicate launches are
blocked. It also adds a harvest endpoint that promotes proven AUTO DISCOVERY
search terms into the matching MANUAL EXACT campaign, plus a small dashboard UI
patch so these controls are visible without rewriting the whole template.
"""
import datetime
import hmac
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, Header, HTTPException
from safety import live_requested
from amazon_results import create_keywords_verified
from fastapi.responses import HTMLResponse, JSONResponse

import server as base
from server import app
from optimize_campaigns import AmazonAdsClient, DEFAULT_FALLBACK_BID, generate_keywords_for_product, load_products, normalized_product, parse_report_json_bytes, verify_internal_token
from budget_dayparting import budget_protection_status, choose_budget_protected_bid
from ppc_waste_rules import classify_search_terms, summarize_classification


DASHBOARD_PATCH_JS = r"""
<script>
(function(){
  function byId(id){ return document.getElementById(id); }
  function fmtMoney(v){ return '$' + Number(v || 0).toFixed(2); }
  function notify(msg, isErr){
    if (typeof toast === 'function') toast(msg, !!isErr);
    else alert(msg);
  }
  function token(){
    if (typeof getToken === 'function') return getToken();
    return (localStorage.getItem('nws_token') || '').trim();
  }
  function apiJson(url, body){
    var t = token();
    if (!t) throw new Error('Missing DAILY_OPTIMIZER_TOKEN');
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + t,
        'X-Daily-Optimizer-Token': t
      },
      body: JSON.stringify(body || {})
    }).then(function(res){
      return res.text().then(function(txt){
        var data = txt ? JSON.parse(txt) : {};
        if (!res.ok || data.error) throw new Error(data.message || data.detail || 'Request failed');
        return data;
      });
    });
  }

  function apiGetJson(url){
    var t = token();
    if (!t) throw new Error('Missing DAILY_OPTIMIZER_TOKEN');
    return fetch(url, {
      headers: {
        'Authorization': 'Bearer ' + t,
        'X-Daily-Optimizer-Token': t
      }
    }).then(function(res){
      return res.text().then(function(txt){
        var data = txt ? JSON.parse(txt) : {};
        if (!res.ok || data.error) throw new Error(data.message || data.detail || 'Request failed');
        return data;
      });
    });
  }

  function isEnabledCampaign(c){
    return String((c && c.state) || '').toUpperCase() === 'ENABLED';
  }

  function forceActiveCampaignsOnly(){
    var filter = byId('stateFilter');
    if (filter) {
      filter.value = 'ENABLED';
      for (var i = filter.options.length - 1; i >= 0; i--) {
        var val = String(filter.options[i].value || '').toUpperCase();
        if (val !== 'ENABLED') filter.remove(i);
      }
    }
    if (Array.isArray(window.allCampaigns)) {
      window.allCampaigns = window.allCampaigns.filter(isEnabledCampaign);
    }
  }

  function patchCampaignRendering(){
    if (typeof window.renderCampaigns === 'function' && !window.renderCampaigns.__activeOnly) {
      var oldRenderCampaigns = window.renderCampaigns;
      window.renderCampaigns = function(campaigns, opts){
        forceActiveCampaignsOnly();
        campaigns = Array.isArray(campaigns) ? campaigns.filter(isEnabledCampaign) : [];
        return oldRenderCampaigns(campaigns, opts);
      };
      window.renderCampaigns.__activeOnly = true;
    }
    if (typeof window.renderCampaignsFromState === 'function' && !window.renderCampaignsFromState.__activeOnly) {
      var oldRenderFromState = window.renderCampaignsFromState;
      window.renderCampaignsFromState = function(){
        forceActiveCampaignsOnly();
        return oldRenderFromState();
      };
      window.renderCampaignsFromState.__activeOnly = true;
    }
    if (typeof window.loadDashboard === 'function' && !window.loadDashboard.__activeOnly) {
      var oldLoadDashboard = window.loadDashboard;
      window.loadDashboard = function(){
        var result = oldLoadDashboard.apply(this, arguments);
        setTimeout(function(){
          forceActiveCampaignsOnly();
          if (typeof window.renderCampaignsFromState === 'function') window.renderCampaignsFromState();
        }, 500);
        return result;
      };
      window.loadDashboard.__activeOnly = true;
    }
  }

  function addHarvestButton(){
    var bar = document.querySelector('.prod-bar') || document.querySelector('#panel-products .toolbar');
    if (!bar || byId('harvestDiscoveryBtn')) return;
    var btn = document.createElement('button');
    btn.id = 'harvestDiscoveryBtn';
    btn.className = 'btn btn-blue';
    btn.textContent = '🌾 Harvest Discovery Winners';
    btn.onclick = function(){
      var sku = prompt('Enter SKU to harvest from AUTO DISCOVERY into MANUAL EXACT:');
      if (!sku) return;
      btn.disabled = true;
      btn.textContent = 'Checking winners...';
      apiJson('/api/harvest-discovery-winners', {
        sku: sku.trim(),
        lookback_days: 14,
        max_terms: 25,
        apply_live: false
      }).then(function(preview){
        var terms = preview.terms_harvested || [];
        var msg = 'Preview for ' + sku + '\n\n' +
          'Rows analyzed: ' + (preview.rows_analyzed || 0) + '\n' +
          'Winners found: ' + (preview.winners_found || 0) + '\n' +
          'New exact terms selected: ' + (preview.terms_selected || 0) + '\n\n' +
          (terms.length ? terms.slice(0,25).join('\n') : 'No new winners to harvest yet.') +
          '\n\nApply live now?';
        if (!terms.length) {
          notify('No discovery winners ready to harvest yet.');
          return null;
        }
        if (!confirm(msg)) return null;
        return apiJson('/api/harvest-discovery-winners', {
          sku: sku.trim(),
          lookback_days: 14,
          max_terms: 25,
          apply_live: true
        });
      }).then(function(result){
        if (!result) return;
        notify('✅ Harvest complete: ' + (result.keywords_created || 0) + ' exact keywords added.');
        if (typeof loadDashboard === 'function') setTimeout(loadDashboard, 1500);
      }).catch(function(err){
        notify('❌ ' + err.message, true);
      }).finally(function(){
        btn.disabled = false;
        btn.textContent = '🌾 Harvest Discovery Winners';
      });
    };
    bar.appendChild(btn);
  }

  var opportunityReturnFocus = null;
  function closeOpportunityQueue(){
    var overlay = byId('opportunityQueueOverlay');
    if (overlay) { if (typeof overlay.close === 'function') overlay.close(); overlay.remove(); }
    if (opportunityReturnFocus && typeof opportunityReturnFocus.focus === 'function') opportunityReturnFocus.focus();
    opportunityReturnFocus = null;
  }

  function opportunityMetric(label, value){
    var row = document.createElement('div');
    row.style.fontSize = '12px';
    row.style.marginTop = '3px';
    row.textContent = label + ': ' + value;
    return row;
  }

  function renderOpportunityQueue(data){
    closeOpportunityQueue();
    opportunityReturnFocus = document.activeElement;
    var overlay = document.createElement('dialog');
    overlay.id = 'opportunityQueueOverlay';
    overlay.setAttribute('aria-labelledby', 'opportunityQueueTitle');
    overlay.style.position = 'fixed';
    overlay.style.inset = '0';
    overlay.style.background = 'rgba(0,0,0,.62)';
    overlay.style.zIndex = '9999';
    overlay.style.overflow = 'auto';
    overlay.style.padding = '28px';

    var panel = document.createElement('div');
    panel.style.maxWidth = '900px';
    panel.style.margin = '0 auto';
    panel.style.background = '#fff';
    panel.style.color = '#111827';
    panel.style.borderRadius = '14px';
    panel.style.padding = '20px';
    overlay.appendChild(panel);

    var head = document.createElement('div');
    head.style.display = 'flex';
    head.style.justifyContent = 'space-between';
    head.style.alignItems = 'center';
    var title = document.createElement('h2');
    title.id = 'opportunityQueueTitle';
    title.textContent = 'Amazon Opportunity Approval Queue';
    title.style.margin = '0';
    head.appendChild(title);
    var close = document.createElement('button');
    close.className = 'btn btn-ghost';
    close.textContent = 'Close';
    close.onclick = closeOpportunityQueue;
    head.appendChild(close);
    panel.appendChild(head);

    var note = document.createElement('p');
    note.textContent = 'Automatic launches require verified product economics. These candidates need your review; confirm costs and profitability before approving additional spending.';
    note.style.fontSize = '13px';
    panel.appendChild(note);

    var approvals = data.approvals || [];
    if (!approvals.length) {
      var empty = document.createElement('div');
      empty.textContent = 'No opportunities are waiting for approval.';
      empty.style.padding = '24px 0';
      panel.appendChild(empty);
    }

    approvals.forEach(function(item){
      var card = document.createElement('div');
      card.style.border = '1px solid #d1d5db';
      card.style.borderRadius = '10px';
      card.style.padding = '14px';
      card.style.marginTop = '12px';

      var name = document.createElement('strong');
      name.textContent = (item.product_title || 'Product') + ' — ' + (item.target || '');
      card.appendChild(name);
      card.appendChild(opportunityMetric('Target type', item.target_type || 'KEYWORD'));
      card.appendChild(opportunityMetric('Margin data', item.economics && ['product_costs','product_margin'].indexOf(item.economics.economics_source) >= 0 ? (Number(item.economics.gross_margin) * 100).toFixed(1) + '%' : 'Missing — profitability unverified'));
      card.appendChild(opportunityMetric('Orders', String(item.orders || 0)));
      card.appendChild(opportunityMetric('Sales', fmtMoney(item.sales || 0)));
      card.appendChild(opportunityMetric('Spend', fmtMoney(item.spend || 0)));
      card.appendChild(opportunityMetric('ACoS', item.acos == null ? '—' : (Number(item.acos) * 100).toFixed(1) + '%'));
      card.appendChild(opportunityMetric('Conversion rate', ((Number(item.conversion_rate || 0)) * 100).toFixed(1) + '%'));

      var actions = document.createElement('div');
      actions.style.marginTop = '12px';
      actions.style.display = 'flex';
      actions.style.gap = '8px';

      var approve = document.createElement('button');
      approve.className = 'btn btn-primary';
      approve.textContent = 'Approve $7/day test';
      approve.onclick = function(){
        approve.disabled = true;
        apiJson('/api/opportunities/approve', {key: item.key, apply_live: true})
          .then(function(){ notify('✅ Opportunity campaign launched.'); return apiGetJson('/api/opportunities'); })
          .then(renderOpportunityQueue)
          .catch(function(err){ notify('❌ ' + err.message, true); approve.disabled = false; });
      };
      actions.appendChild(approve);

      var reject = document.createElement('button');
      reject.className = 'btn btn-ghost';
      reject.textContent = 'Reject';
      reject.onclick = function(){
        reject.disabled = true;
        apiJson('/api/opportunities/reject', {key: item.key, apply_live: true})
          .then(function(){ return apiGetJson('/api/opportunities'); })
          .then(renderOpportunityQueue)
          .catch(function(err){ notify('❌ ' + err.message, true); reject.disabled = false; });
      };
      actions.appendChild(reject);
      card.appendChild(actions);
      panel.appendChild(card);
    });

    overlay.onclick = function(e){ if (e.target === overlay) closeOpportunityQueue(); };
    overlay.addEventListener('cancel', function(e){ e.preventDefault(); closeOpportunityQueue(); });
    document.body.appendChild(overlay);
    overlay.showModal();
    close.focus();
  }

  function addOpportunityButton(){
    var bar = document.querySelector('.prod-bar') || document.querySelector('#panel-products .toolbar');
    if (!bar || byId('opportunityQueueBtn')) return;
    var btn = document.createElement('button');
    btn.id = 'opportunityQueueBtn';
    btn.className = 'btn btn-warn';
    btn.textContent = '🔥 Opportunity Queue';
    btn.onclick = function(){
      btn.disabled = true;
      apiGetJson('/api/opportunities')
        .then(renderOpportunityQueue)
        .catch(function(err){ notify('❌ ' + err.message, true); })
        .finally(function(){ btn.disabled = false; });
    };
    bar.appendChild(btn);
  }

  function improveLaunchText(){
    var launchBtn = byId('launchBtn');
    if (launchBtn) launchBtn.innerHTML = '🚀 Launch AUTO + EXACT';
    var sub = byId('lSub');
    if (sub && /Review and confirm/i.test(sub.textContent || '')) {
      sub.textContent = 'Creates AUTO DISCOVERY + MANUAL EXACT with seed negatives and duplicate protection.';
    }
  }

  window.doLaunch = function(){
    var pid = byId('lPid') && byId('lPid').value;
    if (!pid) return;
    var budget = +(byId('lBudget') && byId('lBudget').value);
    var bid = +(byId('lBid') && byId('lBid').value);
    var discoveryPct = +(byId('lDiscoveryPct') && byId('lDiscoveryPct').value);
    var maxExact = +(byId('lMaxExact') && byId('lMaxExact').value);
    if (!isFinite(budget) || budget < 1) return notify('❌ Daily budget must be at least $1.00', true);
    if (!isFinite(bid) || bid < 0.02) return notify('❌ Starting bid must be at least $0.02', true);
    if (!isFinite(discoveryPct)) discoveryPct = 30;
    if (!isFinite(maxExact)) maxExact = 40;
    discoveryPct = Math.max(10, Math.min(50, discoveryPct));
    maxExact = Math.max(5, Math.min(80, Math.round(maxExact)));

    var btn = byId('launchBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="loader"></span> Launching AUTO + EXACT...'; }
    apiJson('/api/create-campaign-from-product', {
      product_id: pid,
      apply_live: true,
      daily_budget: Number(budget.toFixed(2)),
      starting_bid: Number(bid.toFixed(2)),
      discovery_budget_pct: Number((discoveryPct / 100).toFixed(2)),
      max_exact_keywords: maxExact
    }).then(function(data){
      if (data.duplicate_launch_prevented) {
        notify('✅ Duplicate prevented — existing AUTO DISCOVERY / MANUAL EXACT campaigns found.');
        return;
      }
      var campaigns = data.campaigns_created || [];
      var auto = campaigns.find(function(c){ return c.campaign_type === 'AUTO_DISCOVERY'; }) || {};
      var exact = campaigns.find(function(c){ return c.campaign_type === 'MANUAL_EXACT'; }) || {};
      var negatives = data.launch_negatives ? data.launch_negatives.negative_rows_created : 0;
      notify('✅ Launched AUTO + EXACT. Auto budget ' + fmtMoney(auto.daily_budget) +
        ', Exact budget ' + fmtMoney(exact.daily_budget) +
        ', Exact keywords ' + (exact.keyword_rows_created || 0) +
        ', Seed negatives ' + negatives + '.');
      if (typeof closeModal === 'function') closeModal();
      if (typeof loadDashboard === 'function') setTimeout(loadDashboard, 2500);
    }).catch(function(err){
      notify('❌ ' + err.message, true);
    }).finally(function(){
      if (btn) { btn.disabled = false; btn.innerHTML = '🚀 Launch AUTO + EXACT'; }
    });
  };

  function patch(){
    forceActiveCampaignsOnly();
    patchCampaignRendering();
    addHarvestButton();
    addOpportunityButton();
    improveLaunchText();
    var oldOpen = window.openLaunch;
    if (typeof oldOpen === 'function' && !oldOpen.__nwsPatched) {
      window.openLaunch = function(pid){
        oldOpen(pid);
        setTimeout(improveLaunchText, 50);
      };
      window.openLaunch.__nwsPatched = true;
    }
    if (typeof window.renderCampaignsFromState === 'function') window.renderCampaignsFromState();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', patch);
  else patch();
  setTimeout(patch, 300);
  setTimeout(patch, 1000);
  setInterval(patch, 5000);
})();
</script>
"""


def _remove_route(path: str, method: str) -> None:
    app.router.routes = [
        route for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and method.upper() in set(getattr(route, "methods", set()) or set())
        )
    ]


_remove_route("/api/create-campaign-from-product", "POST")
_remove_route("/", "GET")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_with_extended_controls():
    try:
        html = base.DASHBOARD_PATH.read_text(encoding="utf-8")
        html = html.replace("</body>", DASHBOARD_PATCH_JS + "\n</body>")
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as exc:
        return HTMLResponse(f"<h2>Dashboard Error</h2><p>{exc}</p>", status_code=500)


def _optional_dashboard_auth(authorization: Optional[str], x_daily_optimizer_token: Optional[str]) -> Optional[JSONResponse]:
    try:
        verify_internal_token(authorization, x_daily_optimizer_token)
    except HTTPException as exc:
        return JSONResponse({"error": True, "message": exc.detail}, status_code=exc.status_code)
    return None


def _product_from_key(key: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    key = key.lower().strip()
    for row in load_products():
        if row.get("Product_ID", "").lower() == key or row.get("SKU", "").lower() == key:
            return normalized_product(row), row
    return None, {}


def _safe_title(product: Dict[str, Any]) -> str:
    return base._sanitize_name(str(product.get("title") or "Product"))[:70]


def _find_existing_launch_campaigns(client: AmazonAdsClient, safe_title: str) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    prefix = f"{safe_title} | "
    for campaign in client.list_campaigns():
        name = str(campaign.get("name") or "")
        state = str(campaign.get("state", "")).upper()
        if state not in {"ENABLED", "PAUSED"} or not name.startswith(prefix):
            continue
        if "| AUTO DISCOVERY |" in name and "AUTO_DISCOVERY" not in found:
            found["AUTO_DISCOVERY"] = campaign
        if "| MANUAL EXACT |" in name and "MANUAL_EXACT" not in found:
            found["MANUAL_EXACT"] = campaign
    return found


def _list_ad_groups(client: AmazonAdsClient, campaign_id: str) -> List[Dict[str, Any]]:
    return client.list_ad_groups(campaign_id)


def _first_ad_group_id(client: AmazonAdsClient, campaign_id: str) -> Optional[str]:
    for ad_group in _list_ad_groups(client, campaign_id):
        if ad_group.get("adGroupId"):
            return str(ad_group["adGroupId"])
    return None


def _search_term_rows(client: AmazonAdsClient, lookback_days: int) -> Tuple[List[Dict[str, Any]], str, str, str]:
    start_date = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    end_date = datetime.date.today().isoformat()
    report_id = client.request_report({
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["searchTerm"],
            "columns": ["campaignId", "adGroupId", "searchTerm", "clicks", "cost", "sales7d", "purchases7d"],
            "reportTypeId": "spSearchTerm",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    })
    report_url = client.wait_for_report(report_id, timeout_seconds=1200)
    return parse_report_json_bytes(client.download_binary(report_url)), report_id, start_date, end_date


@app.post("/api/create-campaign-from-product")
def api_create_campaign_with_duplicate_protection(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Launch using server.py logic, but block duplicate product launches first."""
    auth_error = _optional_dashboard_auth(authorization, x_daily_optimizer_token)
    if auth_error:
        return auth_error
    try:
        key = (payload.get("product_id") or payload.get("sku") or "").lower().strip()
        if not key:
            return JSONResponse({"error": True, "message": "product_id or sku required"}, status_code=400)
        product, _ = _product_from_key(key)
        if not product:
            return JSONResponse({"error": True, "message": "Product not found"}, status_code=404)

        client = AmazonAdsClient()
        safe_title = _safe_title(product)
        existing = _find_existing_launch_campaigns(client, safe_title)
        force_relaunch = bool(payload.get("force_relaunch", False))
        apply_live = live_requested(payload)
        complete_pair = (
            len(existing) == 2
            and all(str(campaign.get("state") or "").upper() == "ENABLED"
                    for campaign in existing.values())
        )
        if complete_pair and not force_relaunch:
            return JSONResponse({
                "success": True,
                "duplicate_launch_prevented": True,
                "message": "Matching AUTO DISCOVERY and MANUAL EXACT campaigns already exist. No new campaigns were created.",
                "product": product.get("title"),
                "existing_campaigns": {
                    campaign_type: {
                        "campaign_id": str(campaign.get("campaignId") or ""),
                        "name": campaign.get("name"),
                        "state": campaign.get("state"),
                    }
                    for campaign_type, campaign in existing.items()
                },
            })

        if not apply_live:
            return JSONResponse({
                "success": True,
                "dry_run": True,
                "apply_live": False,
                "duplicate_launch_prevented": False,
                "partial_launch_detected": bool(existing),
                "message": "Launch preview passed validation; no Amazon campaigns were created.",
                "product": product.get("title"),
                "sku": product.get("sku"),
                "asin": product.get("asin"),
            })

        launch_payload = dict(payload)
        if existing and not force_relaunch:
            launch_payload["_resume_campaigns"] = {
                campaign_type: str(campaign.get("campaignId") or "")
                for campaign_type, campaign in existing.items()
            }
        return base.api_create_recommended_campaigns(
            launch_payload, authorization, x_daily_optimizer_token
        )
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/harvest-discovery-winners")
def api_harvest_discovery_winners(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Promote proven AUTO DISCOVERY search terms into MANUAL EXACT."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        key = (payload.get("product_id") or payload.get("sku") or "").lower().strip()
        if not key:
            return JSONResponse({"error": True, "message": "product_id or sku required"}, status_code=400)
        product, _ = _product_from_key(key)
        if not product:
            return JSONResponse({"error": True, "message": "Product not found"}, status_code=404)

        lookback_days = max(1, min(60, int(payload.get("lookback_days", 14))))
        max_terms = max(1, min(100, int(payload.get("max_terms", 25))))
        apply_live = live_requested(payload)
        fallback_bid = float(payload.get("winner_bid", product.get("suggested_bid") or DEFAULT_FALLBACK_BID))
        _, _, protected_bid = choose_budget_protected_bid({}, fallback_bid)
        exact_bid = round(protected_bid, 2)

        client = AmazonAdsClient()
        existing = _find_existing_launch_campaigns(client, _safe_title(product))
        discovery_campaign = existing.get("AUTO_DISCOVERY")
        exact_campaign = existing.get("MANUAL_EXACT")
        if not discovery_campaign or not exact_campaign:
            return JSONResponse({
                "error": True,
                "message": "Could not find both AUTO DISCOVERY and MANUAL EXACT campaigns for this product.",
                "found_campaigns": list(existing.keys()),
            }, status_code=404)

        discovery_campaign_id = str(discovery_campaign.get("campaignId") or "")
        exact_campaign_id = str(exact_campaign.get("campaignId") or "")
        exact_ad_group_id = _first_ad_group_id(client, exact_campaign_id)
        if not exact_ad_group_id:
            return JSONResponse({"error": True, "message": "MANUAL EXACT campaign has no enabled ad group."}, status_code=404)

        rows, report_id, start_date, end_date = _search_term_rows(client, lookback_days)
        discovery_rows = [row for row in rows if str(row.get("campaignId") or "") == discovery_campaign_id]
        classified = classify_search_terms(discovery_rows)
        winners = sorted(
            classified.get("winners", []),
            key=lambda item: (float(item.get("sales") or 0), -float(item.get("acos") or 9)),
            reverse=True,
        )

        existing_exact_terms = {
            base._normalize_keyword(keyword.get("keywordText"))
            for keyword in client.list_keywords(exact_campaign_id)
            if str(keyword.get("matchType") or "").upper() == "EXACT"
            and str(keyword.get("state", "")).upper() != "ARCHIVED"
        }
        selected_terms: List[str] = []
        skipped_existing: List[str] = []
        for item in winners:
            term = base._normalize_keyword(item.get("term"))
            if not term:
                continue
            if term in existing_exact_terms:
                skipped_existing.append(term)
                continue
            selected_terms.append(term)
            existing_exact_terms.add(term)
            if len(selected_terms) >= max_terms:
                break

        keyword_rows = base._exact_keyword_rows(selected_terms, exact_campaign_id, exact_ad_group_id, exact_bid)
        outcome = create_keywords_verified(client, keyword_rows) if apply_live else {
            "accepted": 0, "failed": 0, "success": True, "errors": []}
        created = outcome["accepted"]

        return JSONResponse({
            "success": outcome["success"],
            "keyword_result": outcome,
            "apply_live": apply_live,
            "product": product.get("title"),
            "report_id": report_id,
            "date_range": {"start": start_date, "end": end_date},
            "budget_protection": budget_protection_status(),
            "discovery_campaign_id": discovery_campaign_id,
            "exact_campaign_id": exact_campaign_id,
            "exact_ad_group_id": exact_ad_group_id,
            "rows_analyzed": len(discovery_rows),
            "winners_found": len(winners),
            "terms_selected": len(selected_terms),
            "keywords_created": created,
            "applied_bid": exact_bid,
            "terms_harvested": selected_terms,
            "skipped_existing_sample": skipped_existing[:25],
            "summary": summarize_classification(classified),
        }, status_code=502 if outcome["failed"] else 200)
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
