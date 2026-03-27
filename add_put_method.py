with open('app.py', 'r') as f:
    content = f.read()

# Find the post method and add put after it
old_code = '''    def get(self, endpoint: str) -> Any:
        url = f"{self.base_url}{endpoint}"
        content_type = self._content_type_for(endpoint)
        resp = self.session.get(url, headers=self.headers(content_type), timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Amazon Ads API error {resp.status_code}: {resp.text}")
        return resp.json() if resp.text.strip() else None'''

new_code = '''    def put(self, endpoint: str, body: Any) -> Any:
        """PUT request for updating resources"""
        url = f"{self.base_url}{endpoint}"
        content_type = self._content_type_for(endpoint)
        batch_key = self._batch_key_for(endpoint)
        if batch_key and isinstance(body, list):
            body = {batch_key: body}
        logger.info(f"PUT to {endpoint}")
        logger.info(f"Body preview: {str(body)[:300]}")
        resp = self.session.put(url, headers=self.headers(content_type), json=body, timeout=60)
        logger.info(f"Status: {resp.status_code}")
        if not resp.ok:
            logger.error(f"Error response: {resp.text}")
            raise RuntimeError(f"Amazon Ads API error {resp.status_code}: {resp.text}")
        result = resp.json() if resp.text.strip() else None
        logger.info(f"Response preview: {str(result)[:300]}")
        return result

    def get(self, endpoint: str) -> Any:
        url = f"{self.base_url}{endpoint}"
        content_type = self._content_type_for(endpoint)
        resp = self.session.get(url, headers=self.headers(content_type), timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Amazon Ads API error {resp.status_code}: {resp.text}")
        return resp.json() if resp.text.strip() else None'''

content = content.replace(old_code, new_code)

with open('app.py', 'w') as f:
    f.write(content)

print("✓ Added put method to AmazonAdsClient class")
