import unittest

from performance_checker.profile_phases import PHASES, validate_event


class ProfileEventTests(unittest.TestCase):
    def test_invalid_or_overlapping_counters_are_rejected(self):
        event = {"sequence": 1, "wall_ns": 100, "phases": {name: {"ns": 0, "calls": 0} for name in PHASES}}
        event["phases"]["sql_parse"] = {"ns": 30, "calls": 1}
        event["phases"]["filter"] = {"ns": 70, "calls": 1}
        validate_event(event)
        event["phases"]["filter"]["ns"] = 71
        with self.assertRaises(ValueError):
            validate_event(event)
        event["phases"]["filter"]["ns"] = -1
        with self.assertRaises(ValueError):
            validate_event(event)
        del event["phases"]["filter"]
        with self.assertRaises(ValueError):
            validate_event(event)
