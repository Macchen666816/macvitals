import importlib.util
import pathlib
import unittest


SPEC = importlib.util.spec_from_file_location(
    "compare_probe_tests", pathlib.Path(__file__).parents[1] / "macvitals/compare_probe.py"
)


class CompareProbeTests(unittest.TestCase):
    def test_frames_require_complete_sample_headers(self):
        module = importlib.util.module_from_spec(SPEC)
        # This helper imports package-relative modules only when compare() is
        # used; load it through the package path for the test environment.
        import macvitals.compare_probe as module
        frames = module._frames("header\n*** Sample 1\nCPU Power: 1 mW\n*** Sample 2\nCPU Power: 2 mW\n")
        self.assertEqual(len(frames), 2)
        self.assertTrue(frames[0].startswith("*** Sample 1"))

    def test_unprivileged_compare_is_explicitly_unavailable(self):
        import macvitals.compare_probe as module
        import unittest.mock as mock
        with mock.patch.object(module.os, "geteuid", return_value=501):
            report = module.compare(0.5)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("root", report["error"])

    def test_delta_records_keep_counter_and_state_differences(self):
        import macvitals.compare_probe as module
        previous = [{"channel": "PCPU", "unit": "ticks", "value": 10,
                     "states": [{"name": "IDLE", "residency": 8}]}]
        current = [{"channel": "PCPU", "unit": "ticks", "value": 16,
                    "states": [{"name": "IDLE", "residency": 11}]}]
        self.assertEqual(module._delta_records(previous, current)[0]["value"], 6)
        self.assertEqual(module._delta_records(previous, current)[0]["states"][0]["residency"], 3)


if __name__ == "__main__":
    unittest.main()
