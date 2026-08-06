from fastapi.responses import JSONResponse
from ppc_agent import AmazonPpcAgent


def tool(name, calls):
    def run(payload):
        calls.append((name, payload))
        return JSONResponse({"success": True, "tool": name})
    return run


def make_agent(calls):
    names = ("refresh_dashboard", "audit_acos", "retune_bids", "harvest_keywords", "launch_campaign")
    return AmazonPpcAgent({name: tool(name, calls) for name in names})


def test_agent_defaults_to_safe_dry_run():
    calls = []
    result = make_agent(calls).run({})
    assert result["success"] is True
    assert result["mode"] == "DRY_RUN"
    assert [name for name, _ in calls] == ["retune_bids", "refresh_dashboard", "audit_acos"]
    assert calls[0][1]["apply_live"] is False


def test_live_launch_requires_explicit_guard():
    calls = []
    agent = make_agent(calls)
    blocked = agent.run({"apply_live": True, "actions": ["launch_campaign"], "product_id": "NWS_001"})
    assert blocked["success"] is False
    assert blocked["results"]["launch_campaign"]["status_code"] == 409
    assert calls == []
    allowed = agent.run({"apply_live": True, "actions": ["launch_campaign"], "product_id": "NWS_001", "allow_campaign_launch": True})
    assert allowed["success"] is True
    assert calls[0][1]["apply_live"] is True