"""Offline persistent-state contract tests for GCS locking and restart safety."""
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from automation_store import GCSState, StateBusy

class NotFound(Exception): pass
class PreconditionFailed(Exception): pass

class Blob:
    def __init__(self, name, objects):
        self.name, self.objects, self.generation = name, objects, None
    def upload_from_string(self, content, content_type=None, if_generation_match=None):
        current = self.objects.get(self.name)
        if if_generation_match == 0 and current is not None:
            raise PreconditionFailed()
        self.generation = (current[1] if current else 0) + 1
        self.objects[self.name] = (content, self.generation)
    def download_as_text(self):
        if self.name not in self.objects: raise NotFound()
        return self.objects[self.name][0]
    def delete(self, if_generation_match=None):
        current = self.objects.get(self.name)
        if current is None or current[1] != if_generation_match: raise PreconditionFailed()
        del self.objects[self.name]

class Storage:
    objects = {}
    class Client:
        def bucket(self, name):
            class Bucket:
                def blob(self, path): return Blob(path, Storage.objects)
            return Bucket()

class GCSContractTests(unittest.TestCase):
    def setUp(self):
        Storage.objects = {}
        modules = {"google": types.ModuleType("google"),
                   "google.cloud": types.ModuleType("google.cloud"),
                   "google.cloud.storage": types.ModuleType("google.cloud.storage"),
                   "google.api_core": types.ModuleType("google.api_core"),
                   "google.api_core.exceptions": types.ModuleType("google.api_core.exceptions")}
        modules["google.cloud.storage"].Client = Storage.Client
        modules["google.api_core.exceptions"].NotFound = NotFound
        modules["google.api_core.exceptions"].PreconditionFailed = PreconditionFailed
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.env_patch = patch.dict(os.environ, {"AUTOMATION_STATE_BUCKET": "test-bucket", "AMAZON_ADS_PROFILE_ID": "123"})
        self.env_patch.start()
    def tearDown(self):
        self.env_patch.stop()
        self.module_patch.stop()

    def test_persistence_across_new_instances(self):
        one = GCSState("auto-launch")
        one.save({"intents": {"ASIN": {"status": "uncertain"}}})
        two = GCSState("auto-launch")
        self.assertEqual(two.read()["intents"]["ASIN"]["status"], "uncertain")

    def test_parallel_lock_contention(self):
        one, two = GCSState("auto-launch"), GCSState("auto-launch")
        with one.locked():
            with self.assertRaises(StateBusy):
                with two.locked(): pass

    def test_interrupted_lock_requires_operator_recovery(self):
        one = GCSState("auto-launch")
        one.lock.upload_from_string("orphan", if_generation_match=0)
        with self.assertRaises(StateBusy):
            with GCSState("auto-launch").locked(): pass

if __name__ == "__main__": unittest.main()
