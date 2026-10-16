import unittest
from unittest.mock import patch
from auto_launch_pipeline import run_pipeline
from auto_launch_coordinator import LaunchBlocked

class Coordinator:
    enable = True
    def run_one(self, **kwargs):
        return {'status': 'paused_requires_activation', 'asin': 'B001', 'campaign_id': '42'}

class Guard:
    def authorize_paused_launch(self, *args):
        return {'max_bid': .25, 'budget_cap': 3.0}

class Provisioner:
    def __init__(self, status='incomplete'):
        self.called = 0
        self.status = status
    def provision(self, **kwargs):
        self.called += 1
        return {'status': self.status}

class TestPipeline(unittest.TestCase):
    def test_rejects_incomplete_provisioning(self):
        worker = Provisioner()
        with self.assertRaises(LaunchBlocked):
            run_pipeline(coordinator=Coordinator(), provisioner=worker, store=None,
                adapter=None, guard=Guard(), products=[],
                verified={'B001': {'asin':'B001','sku':'SKU'}},
                policy={'daily_limit':75}, account_profile='123')
        self.assertEqual(worker.called, 1)

    def test_rejects_missing_sku_without_creating_entities(self):
        worker = Provisioner()
        with self.assertRaises(LaunchBlocked):
            run_pipeline(coordinator=Coordinator(), provisioner=worker, store=None,
                adapter=None, guard=Guard(), products=[], verified={},
                policy={'daily_limit':75}, account_profile='123')
        self.assertEqual(worker.called, 0)

    def test_disabled_never_calls_stage(self):
        worker = Provisioner()
        coordinator = Coordinator()
        coordinator.enable = False
        result = run_pipeline(coordinator=coordinator, provisioner=worker,
            store=None, adapter=None, guard=Guard(), products=[], verified={},
            policy={'daily_limit':75}, account_profile='123')
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(worker.called, 0)
