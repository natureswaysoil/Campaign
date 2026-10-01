"""Durable automation state, with an exclusive generation-checked GCS lock.

Locks deliberately do not expire: an interrupted write must not silently permit
another live run. See AUTOMATION_V2.md for recovery after a killed instance.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


class StateBusy(RuntimeError):
    pass


class GCSState:
    def __init__(self, name):
        from google.cloud import storage
        bucket = os.getenv('AUTOMATION_STATE_BUCKET', '').strip()
        if not bucket:
            raise RuntimeError('AUTOMATION_STATE_BUCKET is required for live automation')
        prefix = os.getenv('AMAZON_ADS_PROFILE_ID') or os.getenv('AMAZON_PROFILE_ID')
        if not prefix:
            raise RuntimeError('Amazon profile ID is required for automation state')
        self.bucket = storage.Client().bucket(bucket)
        self.blob = self.bucket.blob(f'{prefix}/{name}.json')
        self.lock = self.bucket.blob(f'{prefix}/{name}.lock')

    def read(self):
        from google.api_core.exceptions import NotFound
        try:
            value = json.loads(self.blob.download_as_text())
            if not isinstance(value, dict):
                raise RuntimeError('Invalid automation state')
            return value
        except NotFound:
            return {}

    def save(self, value):
        self.blob.upload_from_string(json.dumps(value), content_type='application/json')

    @contextmanager
    def locked(self):
        from google.api_core.exceptions import PreconditionFailed
        try:
            self.lock.upload_from_string(json.dumps({'owner': str(uuid4()),
                'started_at': datetime.now(timezone.utc).isoformat()}),
                content_type='application/json', if_generation_match=0)
        except PreconditionFailed as exc:
            raise StateBusy('Automation already running or interrupted; inspect the state lock') from exc
        generation = self.lock.generation
        try:
            yield self.read()
        finally:
            self.lock.delete(if_generation_match=generation)
