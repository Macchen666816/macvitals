import importlib.util
import pathlib
import unittest


SPEC = importlib.util.spec_from_file_location(
    "ioreport_probe_tests", pathlib.Path(__file__).parents[1] / "macvitals/ioreport_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class IOReportProbeTests(unittest.TestCase):
    def test_state_totals_ignore_unavailable_sentinel(self):
        records = [{"states": [
            {"name": "IDLE", "residency": 10},
            {"name": "V0P1", "residency": -9223372036854775808},
        ]}, {"states": [{"name": "IDLE", "residency": 5}]}]
        self.assertEqual(probe._state_totals(records), {"IDLE": 15})

    def test_metadata_does_not_include_sample_values(self):
        records = [{"channel": "ANE0", "unit": "mJ", "states": [], "value": 123}]
        self.assertEqual(probe._channel_metadata(records), [{
            "channel": "ANE0", "unit": "mJ", "states": []
        }])

    def test_counter_total_rejects_unavailable_values(self):
        self.assertEqual(probe._counter_total([{"value": 4}, {"value": -1}, {}]), 4)

    def test_invalid_probe_interval_returns_error(self):
        self.assertEqual(probe.main(["0.1"]), 2)


if __name__ == "__main__":
    unittest.main()
