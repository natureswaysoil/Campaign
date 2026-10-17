import unittest
from unittest.mock import patch
from marketing_read_auth import marketing_read_authorized


class MarketingReadAuthTests(unittest.TestCase):
    @patch.dict('os.environ', {'CAMPAIGN_MARKETING_READ_TOKEN': 'reader-only'})
    def test_only_two_get_routes(self):
        for path in ['/api/profit/status', '/api/profit/eligibility/123']:
            self.assertTrue(marketing_read_authorized('GET', path, 'reader-only'))
            for method in ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']:
                self.assertFalse(marketing_read_authorized(method, path, 'reader-only'))
        for path in ['/api/profit/policy', '/api/profit/data', '/api/create-campaign-from-product', '/api/apply-optimization', '/api/profit/status/extra', '/api/profit/eligibility/foo', '/api/profit/eligibility/123/extra']:
            self.assertFalse(marketing_read_authorized('GET', path, 'reader-only'))
        self.assertFalse(marketing_read_authorized('GET', '/api/profit/status', 'wrong'))
        self.assertFalse(marketing_read_authorized('GET', '/api/profit/status', None))

    @patch.dict('os.environ', {}, clear=True)
    def test_missing_token_fails_closed(self):
        self.assertFalse(marketing_read_authorized('GET', '/api/profit/status', 'reader-only'))


def test_read_credential_cannot_authorize_optimizer_mutations(monkeypatch):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    import final_server
    import optimize_campaigns as core
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'admin-only')
    monkeypatch.setenv('CAMPAIGN_MARKETING_READ_TOKEN', 'reader-only')
    client = TestClient(final_server.app)
    headers = {'X-Marketing-Read-Token': 'reader-only'}
    with patch.object(core, 'AmazonAdsClient') as amazon:
        amazon.return_value.list_ad_groups.return_value = []
        assert client.get('/api/profit/eligibility/123', headers=headers).status_code == 409
        amazon.reset_mock()
        for path in ['/api/update-campaign-state', '/api/create-campaign-from-product', '/api/profit/policy']:
            assert client.post(path, headers=headers, json={'apply_live': True}).status_code == 403
        assert client.get('/api/dashboard-data', headers=headers).status_code == 403
        amazon.assert_not_called()
