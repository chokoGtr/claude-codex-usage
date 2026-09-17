# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest

import codex_usage as xu


RATE_LIMITS = {
    "primary": {"used_percent": 18, "window_minutes": 300, "resets_at": 1789638218},
    "secondary": {"used_percent": 3, "window_minutes": 10080, "resets_at": 1790225018},
}


class ExtractWindowsTest(unittest.TestCase):
    def test_extracts_five_hour_and_weekly(self):
        windows = xu.extract_windows(RATE_LIMITS)
        self.assertEqual([(w.label, w.percent) for w in windows], [("5時間枠", 18.0), ("週次", 3.0)])
        self.assertTrue(windows[0].resets_at.startswith("2026-"))

    def test_unknown_and_malformed_windows(self):
        data = {
            "primary": {"used_percent": True, "window_minutes": 300},
            "secondary": {"used_percent": 4, "window_minutes": 60},
        }
        self.assertEqual(xu.extract_windows(data)[0].label, "60分枠")
        self.assertEqual(xu.extract_windows(None), [])


class ReadLatestUsageTest(unittest.TestCase):
    def _write(self, directory, name, records):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        return path

    def test_reads_last_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, "rollout-a.jsonl", [
                {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": RATE_LIMITS}},
                {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": dict(RATE_LIMITS, primary={"used_percent": 21, "window_minutes": 300})}},
            ])
            data = xu.read_latest_usage(os.path.join(directory, "*.jsonl"))
        self.assertEqual(data["primary"]["used_percent"], 21)

    def test_missing_sessions_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(xu.CodexUsageError):
                xu.read_latest_usage(os.path.join(directory, "*.jsonl"))

    def test_ignores_invalid_json_and_unrelated_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rollout-a.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("not-json\n")
                stream.write(json.dumps({"payload": {"type": "message", "rate_limits": RATE_LIMITS}}) + "\n")
            with self.assertRaises(xu.CodexUsageError):
                xu.read_latest_usage(os.path.join(directory, "*.jsonl"))


if __name__ == "__main__":
    unittest.main()
