import os
import unittest
from unittest.mock import patch
from auto_launch_tick import launch_tick
from auto_launch_coordinator import LaunchBlocked
class TickTests(unittest.TestCase):
    def test_disabled_has_no_side_effects(self):
        with patch.dict(os.environ, {"AUTO_LAUNCH_ENABLED":"false"}):
            self.assertEqual(launch_tick()["status"], "disabled")
    def test_enabled_requires_all_adapters(self):
        with patch.dict(os.environ, {"AUTO_LAUNCH_ENABLED":"true"}):
            with self.assertRaises(LaunchBlocked):launch_tick()
    def test_does_not_accept_unapproved_coordinator(self):
        with patch.dict(os.environ, {"AUTO_LAUNCH_ENABLED":"true"}):
            with self.assertRaises(LaunchBlocked):
                launch_tick(coordinator_factory=lambda: type("X",(),{"enable":False})(),
                            inputs_factory=lambda: {})
if __name__=="__main__":unittest.main()
