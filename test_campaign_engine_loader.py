import requests

import campaign_engine


CSV = "SKU,ASIN,Title\nSKU-1,B000000001,Test Product\n"


class Response:
    text = CSV

    def raise_for_status(self):
        return None


def test_sheet_loader_retries_a_timeout(monkeypatch):
    calls = []

    def get(url, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise requests.ReadTimeout("slow response")
        return Response()

    monkeypatch.setattr(campaign_engine.requests, "get", get)
    monkeypatch.setattr(campaign_engine.time, "sleep", lambda _: None)

    rows = campaign_engine.load_products_from_sheet(
        "https://example.com/products.csv", attempts=3, timeout=(1, 2)
    )

    assert rows[0]["SKU"] == "SKU-1"
    assert len(calls) == 2


def test_sheet_loader_uses_google_alternate_endpoint(monkeypatch):
    calls = []

    def get(url, timeout):
        calls.append(url)
        if "export?format=csv" in url:
            raise requests.ReadTimeout("slow response")
        return Response()

    monkeypatch.setattr(campaign_engine.requests, "get", get)
    monkeypatch.setattr(campaign_engine.time, "sleep", lambda _: None)

    rows = campaign_engine.load_products_from_sheet(
        "https://docs.google.com/spreadsheets/d/sheet123/export?format=csv",
        attempts=1,
    )

    assert rows[0]["ASIN"] == "B000000001"
    assert calls[-1].endswith("/gviz/tq?tqx=out:csv")


def test_sheet_loader_reports_exhausted_retries(monkeypatch):
    monkeypatch.setattr(
        campaign_engine.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ReadTimeout("still slow")
        ),
    )
    monkeypatch.setattr(campaign_engine.time, "sleep", lambda _: None)

    try:
        campaign_engine.load_products_from_sheet(
            "https://example.com/products.csv", attempts=2
        )
    except RuntimeError as exc:
        assert "after 2 attempts" in str(exc)
    else:
        raise AssertionError("Expected a RuntimeError")
