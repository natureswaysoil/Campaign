"""Profitability-gated Amazon opportunity monitoring and launch helpers.

Strong opportunities are allowed to create small, verified test campaigns.
Borderline opportunities are persisted for explicit approval.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi.responses import JSONResponse

from amazon_results import batch_outcome, create_keywords_verified
from automation_store import GCSState, StateBusy
from budget_dayparting import choose_budget_protected_bid
from optimize_campaigns import (
    AmazonAdsClient,
    DEFAULT_FALLBACK_BID,
    _build_search_term_report_body,
    load_products,
    normalized_product,
    parse_report_json_bytes,
)
from safety import live_requested
from scheduled_harvest import eastern_day
import server as launch_server
import extended_server

AUTO_MIN_CLICKS = int(os.getenv("OPPORTUNITY_AUTO_MIN_CLICKS", "8"))
AUTO_MIN_ORDERS = int(os.getenv("OPPORTUNITY_AUTO_MIN_ORDERS", "2"))
AUTO_MIN_SALES = float(os.getenv("OPPORTUNITY_AUTO_MIN_SALES", "40"))
AUTO_MAX_ACOS = float(os.getenv("OPPORTUNITY_AUTO_MAX_ACOS", "0.35"))
AUTO_MIN_CVR = float(os.getenv("OPPORTUNITY_AUTO_MIN_CVR", "0.10"))

APPROVAL_MIN_CLICKS = int(os.getenv("OPPORTUNITY_APPROVAL_MIN_CLICKS", "4"))
APPROVAL_MIN_ORDERS = int(os.getenv("OPPORTUNITY_APPROVAL_MIN_ORDERS", "1"))
APPROVAL_MIN_SALES = float(os.getenv("OPPORTUNITY_APPROVAL_MIN_SALES", "20"))
APPROVAL_MAX_ACOS = float(os.getenv("OPPORTUNITY_APPROVAL_MAX_ACOS", "0.60"))
APPROVAL_MIN_CVR = float(os.getenv("OPPORTUNITY_APPROVAL_MIN_CVR", "0.05"))

TEST_DAILY_BUDGET = float(os.getenv("OPPORTUNITY_TEST_DAILY_BUDGET", "7.00"))
MAX_AUTO_LAUNCHES_PER_DAY = int(os.getenv("OPPORTUNITY_MAX_AUTO_LAUNCHES_PER_DAY", "3"))
ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$", re.I)


def _number(row: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(str(value).replace("$", "").replace(",", "").strip())
            except Exception:
                pass
    return 0.0


def _term(row: Dict[str, Any]) -> str:
    return str(
        row.get("searchTerm")
        or row.get("Customer Search Term")
        or row.get("Search Term")
        or ""
    ).strip()


def _campaign_id(row: Dict[str, Any]) -> str:
    return str(row.get("campaignId") or row.get("Campaign Id") or "").strip()


def opportunity_key(product_id: str, target_type: str, target: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", str(target).lower()).strip("-")[:80]
    return f"{product_id.lower()}:{target_type.lower()}:{clean}"


def aggregate_opportunities(
    rows: List[Dict[str, Any]],
    discovery_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        cid = _campaign_id(row)
        product = discovery_map.get(cid)
        term = _term(row)
        if not product or not term:
            continue
        key = (cid, term.lower())
        item = aggregated.setdefault(key, {
            "product_id": str(product.get("product_id") or product.get("sku") or product.get("asin") or ""),
            "product_title": product.get("title") or "Product",
            "sku": product.get("sku") or "",
            "asin": product.get("asin") or "",
            "suggested_bid": float(product.get("suggested_bid") or DEFAULT_FALLBACK_BID),
            "source_campaign_id": cid,
            "target": term,
            "target_type": "ASIN" if ASIN_RE.match(term) else "KEYWORD",
            "clicks": 0,
            "orders": 0,
            "spend": 0.0,
            "sales": 0.0,
        })
        item["clicks"] += int(_number(row, "clicks", "Clicks"))
        item["orders"] += int(_number(
            row, "purchases7d", "purchases14d", "orders",
            "7 Day Total Orders (#)", "14 Day Total Orders (#)"
        ))
        item["spend"] += _number(row, "cost", "spend", "Spend", "Cost")
        item["sales"] += _number(
            row, "sales7d", "sales14d", "sales",
            "7 Day Total Sales", "14 Day Total Sales"
        )

    result: List[Dict[str, Any]] = []
    for item in aggregated.values():
        clicks = int(item["clicks"])
        orders = int(item["orders"])
        sales = round(float(item["sales"]), 2)
        spend = round(float(item["spend"]), 2)
        cvr = (orders / clicks) if clicks else 0.0
        acos = (spend / sales) if sales > 0 else None
        item.update(
            spend=spend,
            sales=sales,
            conversion_rate=round(cvr, 4),
            acos=round(acos, 4) if acos is not None else None,
        )
        item["key"] = opportunity_key(item["product_id"], item["target_type"], item["target"])
        result.append(item)
    return result


def classify_opportunity(item: Dict[str, Any]) -> Dict[str, Any]:
    clicks = int(item.get("clicks") or 0)
    orders = int(item.get("orders") or 0)
    sales = float(item.get("sales") or 0)
    acos = item.get("acos")
    cvr = float(item.get("conversion_rate") or 0)

    auto = (
        clicks >= AUTO_MIN_CLICKS
        and orders >= AUTO_MIN_ORDERS
        and sales >= AUTO_MIN_SALES
        and acos is not None
        and float(acos) <= AUTO_MAX_ACOS
        and cvr >= AUTO_MIN_CVR
    )
    approval = (
        clicks >= APPROVAL_MIN_CLICKS
        and orders >= APPROVAL_MIN_ORDERS
        and sales >= APPROVAL_MIN_SALES
        and acos is not None
        and float(acos) <= APPROVAL_MAX_ACOS
        and cvr >= APPROVAL_MIN_CVR
    )
    if auto:
        decision = "AUTO_LAUNCH"
    elif approval:
        decision = "APPROVAL"
    else:
        decision = "IGNORE"
    return {
        **item,
        "decision": decision,
        "profitability_rules": {
            "auto": {
                "min_clicks": AUTO_MIN_CLICKS,
                "min_orders": AUTO_MIN_ORDERS,
                "min_sales": AUTO_MIN_SALES,
                "max_acos": AUTO_MAX_ACOS,
                "min_conversion_rate": AUTO_MIN_CVR,
            },
            "approval": {
                "min_clicks": APPROVAL_MIN_CLICKS,
                "min_orders": APPROVAL_MIN_ORDERS,
                "min_sales": APPROVAL_MIN_SALES,
                "max_acos": APPROVAL_MAX_ACOS,
                "min_conversion_rate": APPROVAL_MIN_CVR,
            },
        },
    }


def build_discovery_map(client: AmazonAdsClient) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for raw in load_products():
        product = normalized_product(raw)
        existing = extended_server._find_existing_launch_campaigns(
            client, extended_server._safe_title(product)
        )
        campaign = existing.get("AUTO_DISCOVERY")
        if (
            campaign
            and campaign.get("campaignId")
            and str(campaign.get("state") or "").upper() == "ENABLED"
        ):
            mapping[str(campaign["campaignId"])] = product
    return mapping


def _target_row(
    target: str,
    campaign_id: str,
    ad_group_id: str,
    bid: float,
) -> Dict[str, Any]:
    return {
        "campaignId": str(campaign_id),
        "adGroupId": str(ad_group_id),
        "expression": [{"type": "ASIN_SAME_AS", "value": str(target).upper()}],
        "expressionType": "MANUAL",
        "state": "ENABLED",
        "bid": round(float(bid), 2),
    }


def _campaign_name(product_title: str, target_type: str, target: str) -> str:
    safe_title = launch_server._sanitize_name(product_title)[:55]
    safe_target = launch_server._sanitize_name(target)[:35]
    return f"{safe_title} | OPPORTUNITY {target_type} | {safe_target}"[:128]


def _existing_opportunity_campaign(
    client: AmazonAdsClient,
    product_title: str,
    target_type: str,
    target: str,
) -> Optional[Dict[str, Any]]:
    wanted = _campaign_name(product_title, target_type, target)
    for campaign in client.list_campaigns():
        if str(campaign.get("name") or "") == wanted and str(campaign.get("state") or "").upper() != "ARCHIVED":
            return campaign
    return None


def launch_opportunity(
    client: AmazonAdsClient,
    item: Dict[str, Any],
    daily_budget: float = TEST_DAILY_BUDGET,
) -> Dict[str, Any]:
    existing = _existing_opportunity_campaign(
        client, item["product_title"], item["target_type"], item["target"]
    )

    _, _, protected_bid = choose_budget_protected_bid(
        {}, float(item.get("suggested_bid") or DEFAULT_FALLBACK_BID)
    )
    bid = round(max(0.10, protected_bid), 2)
    start_date = datetime.date.today().isoformat()
    campaign_name = _campaign_name(
        item["product_title"], item["target_type"], item["target"]
    )

    if existing:
        campaign_id = str(existing.get("campaignId") or "")
        groups = client.list_ad_groups(campaign_id)
        ad_group_id = str(groups[0].get("adGroupId") or "") if groups else ""
        if not ad_group_id:
            ad_group_id = launch_server._create_ad_group(
                client, campaign_id, "Opportunity Test", bid
            )
        launch_server._ensure_product_ad(
            client, campaign_id, ad_group_id, item.get("sku") or "", item.get("asin") or ""
        )
    else:
        campaign_id = launch_server._create_campaign(
            client, campaign_name, "MANUAL", max(2.0, float(daily_budget)), start_date
        )
        ad_group_id = launch_server._create_ad_group(
            client, campaign_id, "Opportunity Test", bid
        )
        launch_server._create_product_ad(
            client, campaign_id, ad_group_id, item.get("sku") or "", item.get("asin") or ""
        )

    if item["target_type"] == "ASIN":
        wanted = str(item["target"]).upper()
        present = False
        if existing:
            for target in client.list_targets(campaign_id):
                if str(target.get("state") or "").upper() == "ARCHIVED":
                    continue
                for expression in target.get("expression") or []:
                    if (
                        str(expression.get("type") or "").upper() == "ASIN_SAME_AS"
                        and str(expression.get("value") or "").upper() == wanted
                    ):
                        present = True
                        break
                if present:
                    break
        if present:
            target_result = {
                "submitted": 0, "accepted": 0, "failed": 0,
                "errors": [], "unconfirmed": 0, "success": True,
            }
        else:
            row = _target_row(item["target"], campaign_id, ad_group_id, bid)
            response = client.create_targets([row])
            target_result = batch_outcome(response, "targetingClauses", 1)
            if not target_result["success"]:
                raise RuntimeError(f"Opportunity ASIN target was not acknowledged by Amazon: {target_result}")
    else:
        wanted = launch_server._normalize_keyword(item["target"])
        present = False
        if existing:
            present = any(
                launch_server._normalize_keyword(keyword.get("keywordText")) == wanted
                and str(keyword.get("matchType") or "").upper() == "EXACT"
                and str(keyword.get("state") or "").upper() != "ARCHIVED"
                for keyword in client.list_keywords(campaign_id)
            )
        if present:
            target_result = {
                "submitted": 0, "accepted": 0, "failed": 0,
                "errors": [], "unconfirmed": 0, "success": True,
            }
        else:
            row = launch_server._exact_keyword_rows(
                [item["target"]], campaign_id, ad_group_id, bid
            )
            target_result = create_keywords_verified(client, row)
            if not target_result["success"]:
                raise RuntimeError(f"Opportunity exact keyword was not acknowledged by Amazon: {target_result}")

    if not existing or str(existing.get("state") or "").upper() != "ENABLED":
        launch_server._set_campaign_state_verified(client, campaign_id, "ENABLED")
    return {
        "success": True,
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "campaign_name": campaign_name,
        "target_type": item["target_type"],
        "target": item["target"],
        "daily_budget": round(max(2.0, float(daily_budget)), 2),
        "bid": bid,
        "target_result": target_result,
    }


def process_opportunities(
    rows: List[Dict[str, Any]],
    client: AmazonAdsClient,
    state: Dict[str, Any],
    live: bool,
) -> Dict[str, Any]:
    discovery_map = build_discovery_map(client)
    evaluated = [
        classify_opportunity(item)
        for item in aggregate_opportunities(rows, discovery_map)
    ]
    evaluated.sort(
        key=lambda x: (
            int(x.get("orders") or 0),
            float(x.get("sales") or 0),
            -(float(x.get("acos")) if x.get("acos") is not None else 99.0),
        ),
        reverse=True,
    )

    launched = state.setdefault("launched", {})
    approvals = state.setdefault("approvals", {})
    rejected = state.setdefault("rejected", {})
    ignored = 0
    auto_results = []
    auto_count = 0

    for item in evaluated:
        key = item["key"]
        if key in launched or key in rejected:
            continue
        if item["decision"] == "AUTO_LAUNCH":
            if auto_count >= MAX_AUTO_LAUNCHES_PER_DAY:
                approvals[key] = {**item, "reason": "daily_auto_launch_limit"}
                continue
            if live:
                try:
                    result = launch_opportunity(client, item)
                except Exception as exc:
                    approvals[key] = {
                        **item,
                        "reason": "auto_launch_failed",
                        "launch_error": str(exc),
                        "queued_at": time.time(),
                    }
                    continue
                if not result.get("success"):
                    approvals[key] = {**item, "reason": "auto_launch_failed", "launch_result": result}
                    continue
                launched[key] = {
                    "opportunity": item,
                    "launch_result": result,
                    "launched_at": time.time(),
                    "source": "automatic",
                }
                approvals.pop(key, None)
                auto_results.append({"key": key, **result})
                auto_count += 1
            else:
                auto_results.append({"key": key, "preview": True})
        elif item["decision"] == "APPROVAL":
            approvals[key] = {**item, "queued_at": approvals.get(key, {}).get("queued_at", time.time())}
        else:
            approvals.pop(key, None)
            ignored += 1

    return {
        "success": True,
        "live": live,
        "evaluated": len(evaluated),
        "auto_launch_candidates": sum(1 for x in evaluated if x["decision"] == "AUTO_LAUNCH"),
        "approval_candidates": sum(1 for x in evaluated if x["decision"] == "APPROVAL"),
        "ignored": ignored,
        "auto_launches": auto_results,
        "approval_queue": list(approvals.values()),
        "rules": {
            "test_daily_budget": TEST_DAILY_BUDGET,
            "max_auto_launches_per_day": MAX_AUTO_LAUNCHES_PER_DAY,
            "auto": {
                "min_clicks": AUTO_MIN_CLICKS,
                "min_orders": AUTO_MIN_ORDERS,
                "min_sales": AUTO_MIN_SALES,
                "max_acos": AUTO_MAX_ACOS,
                "min_conversion_rate": AUTO_MIN_CVR,
            },
            "approval": {
                "min_clicks": APPROVAL_MIN_CLICKS,
                "min_orders": APPROVAL_MIN_ORDERS,
                "min_sales": APPROVAL_MIN_SALES,
                "max_acos": APPROVAL_MAX_ACOS,
                "min_conversion_rate": APPROVAL_MIN_CVR,
            },
        },
    }


def opportunity_tick(payload: Dict[str, Any]) -> JSONResponse:
    if not live_requested(payload):
        return JSONResponse({
            "success": True,
            "dry_run": True,
            "message": "Opportunity monitor preview; no state or ads changed",
            "rules": classify_opportunity({
                "clicks": 0, "orders": 0, "sales": 0, "acos": None,
                "conversion_rate": 0,
            })["profitability_rules"],
        })
    try:
        store = GCSState("opportunity-workflow")
        with store.locked() as state:
            today = eastern_day()
            if state.get("completed_day") == today.isoformat():
                return JSONResponse({
                    "success": True,
                    "status": "already_completed",
                    "last_result": state.get("last_result"),
                })

            client = AmazonAdsClient()
            pending = state.get("pending")
            if not pending:
                body = _build_search_term_report_body(14)
                body["startDate"] = (today - datetime.timedelta(days=14)).isoformat()
                body["endDate"] = (today - datetime.timedelta(days=1)).isoformat()
                report_id = client.request_report(body)
                state["pending"] = {
                    "report_id": report_id,
                    "day": today.isoformat(),
                    "start": body["startDate"],
                    "end": body["endDate"],
                    "requested_at": time.time(),
                }
                store.save(state)
                return JSONResponse(
                    {"success": True, "status": "requested", "report_id": report_id},
                    status_code=202,
                )

            report = client.get(
                "/reporting/reports/" + pending["report_id"], accept="application/json"
            )
            phase = str(report.get("status") or "").upper()
            if phase in {"FAILED", "FAILURE", "CANCELLED"}:
                state["last_failure"] = {"report_id": pending["report_id"], "status": phase}
                state.pop("pending", None)
                store.save(state)
                return JSONResponse({"success": False, "status": phase}, status_code=502)
            if phase not in {"COMPLETED", "SUCCESS"}:
                return JSONResponse(
                    {"success": True, "status": phase or "pending"}, status_code=202
                )
            url = report.get("url") or report.get("location")
            if not url:
                raise RuntimeError("Completed opportunity report has no download URL")
            rows = parse_report_json_bytes(client.download_binary(url))
            result = process_opportunities(rows, client, state, live=True)
            state["last_result"] = result
            state["last_run_at"] = time.time()
            state["completed_day"] = pending["day"]
            state.pop("pending", None)
            store.save(state)
            return JSONResponse(result)
    except StateBusy as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=503)


def get_queue() -> Dict[str, Any]:
    state = GCSState("opportunity-workflow").read()
    return {
        "success": True,
        "approvals": list((state.get("approvals") or {}).values()),
        "launched": list((state.get("launched") or {}).values()),
        "last_result": state.get("last_result"),
    }


def approve_opportunity(key: str, payload: Dict[str, Any]) -> JSONResponse:
    if not live_requested(payload):
        return JSONResponse({
            "success": True,
            "dry_run": True,
            "key": key,
            "message": "Approval preview; no campaign created",
        })
    try:
        store = GCSState("opportunity-workflow")
        with store.locked() as state:
            approvals = state.setdefault("approvals", {})
            item = approvals.get(key)
            if not item:
                return JSONResponse(
                    {"error": True, "message": "Opportunity is not in the approval queue"},
                    status_code=404,
                )
            client = AmazonAdsClient()
            result = launch_opportunity(
                client, item, float(payload.get("daily_budget") or TEST_DAILY_BUDGET)
            )
            if result.get("success"):
                state.setdefault("launched", {})[key] = {
                    "opportunity": item,
                    "launch_result": result,
                    "launched_at": time.time(),
                    "source": "manual_approval",
                }
                approvals.pop(key, None)
                store.save(state)
            return JSONResponse(result, status_code=200 if result.get("success") else 502)
    except StateBusy as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=503)


def reject_opportunity(key: str) -> JSONResponse:
    try:
        store = GCSState("opportunity-workflow")
        with store.locked() as state:
            approvals = state.setdefault("approvals", {})
            item = approvals.pop(key, None)
            if not item:
                return JSONResponse(
                    {"error": True, "message": "Opportunity is not in the approval queue"},
                    status_code=404,
                )
            state.setdefault("rejected", {})[key] = {
                "opportunity": item,
                "rejected_at": time.time(),
            }
            store.save(state)
            return JSONResponse({"success": True, "key": key, "status": "rejected"})
    except StateBusy as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=503)
