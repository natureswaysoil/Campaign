"""A dedicated credential accepted only by two read-only diagnostic routes."""
import hmac
import os
import re


def marketing_read_authorized(method: str, path: str, supplied: str | None) -> bool:
    expected = os.environ.get('CAMPAIGN_MARKETING_READ_TOKEN', '')
    allowed_path = path == '/api/profit/status' or re.fullmatch(r'/api/profit/eligibility/[0-9]+', path) is not None
    return bool(method == 'GET' and allowed_path and expected and supplied and hmac.compare_digest(supplied, expected))
