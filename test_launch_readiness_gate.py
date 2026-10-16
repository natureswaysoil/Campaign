import unittest
from unittest.mock import patch
from auto_launch_tick import launch_tick

class LaunchReadinessGateTests(unittest.TestCase):
    def test_launch_disabled_without_server_opt_in(self):
        with patch.dict('os.environ', {'AUTO_LAUNCH_ENABLED': 'false'}):
            self.assertEqual(launch_tick()['status'], 'disabled')

    def test_no_automatic_launch_when_no_runtime_adapters(self):
        with patch.dict('os.environ', {'AUTO_LAUNCH_ENABLED': 'true'}):
            with self.assertRaises(Exception):
                launch_tick()

if __name__ == '__main__':
    unittest.main()
