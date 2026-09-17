# -*- coding: utf-8 -*-
"""GUI 版のうち tkinter に依存しない部分のテスト。ウィンドウは開かない。"""

import os
import struct
import unittest
from unittest import mock

import claude_usage as cu
import claude_codex_usage_gui as gui

SAMPLE = {"limits": [{"kind": "session", "percent": 40, "resets_at": None, "is_active": False}]}


class IconAssetTest(unittest.TestCase):
    def test_official_product_icons_are_bundled_at_render_resolution(self):
        for name in ("claude", "codex"):
            path = os.path.join(gui.ASSET_DIR, name + ".png")
            with open(path, "rb") as image:
                header = image.read(24)
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", header[16:24]), (32, 32))


class Recorder(object):
    """publish / wait / should_stop を記録し、規定回数で停止させる。"""

    def __init__(self, stop_after):
        self.updates = []
        self.delays = []
        self.stop_after = stop_after

    def publish(self, update):
        self.updates.append(update)

    def wait(self, seconds):
        self.delays.append(seconds)

    def should_stop(self):
        return len(self.delays) >= self.stop_after


class FetchLoopTest(unittest.TestCase):
    def test_publishes_windows_on_success(self):
        poller = mock.Mock()
        poller.poll.return_value = SAMPLE
        rec = Recorder(stop_after=1)
        gui.fetch_loop(poller, 60, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual(len(rec.updates), 1)
        self.assertEqual([w.label for w in rec.updates[0].windows], ["5時間枠"])
        self.assertIsNone(rec.updates[0].error)
        self.assertIsNotNone(rec.updates[0].at)
        self.assertEqual(rec.delays, [60])

    def test_usage_error_keeps_windows_none_and_backs_off(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.UsageError(500, "HTTP 500")
        rec = Recorder(stop_after=2)
        gui.fetch_loop(poller, 30, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual([u.error for u in rec.updates], ["HTTP 500", "HTTP 500"])
        self.assertEqual([u.windows for u in rec.updates], [None, None])
        self.assertEqual(rec.delays, [30, 60])
        self.assertEqual(poller.invalidate.call_count, 0)

    def test_retry_after_is_honoured(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.UsageError(429, "HTTP 429", retry_after=200)
        rec = Recorder(stop_after=1)
        gui.fetch_loop(poller, 60, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual(rec.delays, [200])

    def test_rate_limit_backs_off_beyond_normal_cap(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.UsageError(429, "HTTP 429")
        rec = Recorder(stop_after=4)
        gui.fetch_loop(poller, 300, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual(rec.delays, [300, 600, 1200, 1800])

    def test_credentials_error_invalidates_poller(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.CredentialsError("読めません")
        rec = Recorder(stop_after=1)
        gui.fetch_loop(poller, 60, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual(rec.updates[0].error, "読めません")
        self.assertEqual(poller.invalidate.call_count, 1)

    def test_failure_count_resets_after_success(self):
        poller = mock.Mock()
        poller.poll.side_effect = [cu.UsageError(500, "HTTP 500"), cu.UsageError(500, "HTTP 500"), SAMPLE, SAMPLE]
        rec = Recorder(stop_after=4)
        gui.fetch_loop(poller, 30, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual(rec.delays, [30, 60, 30, 30])

    def test_stops_before_polling_when_already_stopped(self):
        poller = mock.Mock()
        rec = Recorder(stop_after=0)
        gui.fetch_loop(poller, 60, rec.publish, rec.wait, rec.should_stop)
        self.assertEqual(poller.poll.call_count, 0)

    def test_no_token_in_published_updates(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.UsageError(401, cu.MSG_EXPIRED)
        rec = Recorder(stop_after=1)
        gui.fetch_loop(poller, 60, rec.publish, rec.wait, rec.should_stop)
        self.assertNotIn("Bearer", repr(rec.updates))
        self.assertNotIn("sk-ant", repr(rec.updates))

    def test_manual_refresh_interrupts_wait_and_polls_again(self):
        poller = mock.Mock()
        poller.poll.return_value = SAMPLE
        control = gui.FetchControl()
        updates = []

        def publish(update):
            updates.append(update)
            if len(updates) == 1:
                control.request_refresh()
            else:
                control.stop()

        gui.fetch_loop(poller, 3600, publish, control.wait, control.is_stopped)
        self.assertEqual(poller.poll.call_count, 2)
        self.assertEqual(len(updates), 2)

    def test_repeated_manual_refresh_requests_are_coalesced(self):
        poller = mock.Mock()
        poller.poll.return_value = SAMPLE
        control = gui.FetchControl()
        updates = []

        def publish(update):
            updates.append(update)
            if len(updates) == 1:
                control.request_refresh()
                control.request_refresh()
            else:
                control.stop()

        gui.fetch_loop(poller, 3600, publish, control.wait, control.is_stopped)
        self.assertEqual(poller.poll.call_count, 2)


class UsageWindowRefreshTest(unittest.TestCase):
    def test_refresh_updates_ui_and_wakes_worker(self):
        window = object.__new__(gui.UsageWindow)
        window.sources = {"claude": gui.UsageWindow._source("Claude Usage")}
        source = window.sources["claude"]
        source["refreshing"] = False
        source["control"].request_refresh = mock.Mock()
        window.redraw = mock.Mock()

        self.assertEqual(window.refresh_source("claude"), "break")
        self.assertTrue(source["refreshing"])
        source["control"].request_refresh.assert_called_once_with()
        window.redraw.assert_called_once_with()

    def test_refresh_source_wakes_only_requested_worker(self):
        window = object.__new__(gui.UsageWindow)
        window.sources = {
            "claude": gui.UsageWindow._source("Claude Usage"),
            "codex": gui.UsageWindow._source("Codex Usage"),
        }
        for source in window.sources.values():
            source["refreshing"] = False
            source["control"].request_refresh = mock.Mock()
        window.redraw = mock.Mock()

        window.refresh_source("codex")
        window.sources["codex"]["control"].request_refresh.assert_called_once_with()
        window.sources["claude"]["control"].request_refresh.assert_not_called()
        window.redraw.assert_called_once_with()

    def test_keyboard_refresh_wakes_both_workers(self):
        window = object.__new__(gui.UsageWindow)
        window.sources = {
            "claude": gui.UsageWindow._source("Claude Usage"),
            "codex": gui.UsageWindow._source("Codex Usage"),
        }
        window.refresh_source = mock.Mock(return_value="break")

        self.assertEqual(window.refresh(), "break")
        self.assertEqual(
            window.refresh_source.call_args_list,
            [mock.call("claude"), mock.call("codex")],
        )

    def test_refresh_while_fetching_does_not_queue_another_request(self):
        window = object.__new__(gui.UsageWindow)
        window.sources = {"claude": gui.UsageWindow._source("Claude Usage")}
        source = window.sources["claude"]
        source["control"].request_refresh = mock.Mock()

        self.assertEqual(window.refresh_source("claude"), "break")
        source["control"].request_refresh.assert_not_called()


class PaletteTest(unittest.TestCase):
    def test_dark_background(self):
        self.assertEqual(gui.palette_for((12850, 12850, 12850))["track"], gui.DARK["track"])

    def test_light_background(self):
        self.assertEqual(gui.palette_for((60000, 60000, 60000))["track"], gui.LIGHT["track"])

    def test_boundary_uses_luma_not_raw_channels(self):
        # 緑が支配的な中間色は明るいと判定される
        self.assertEqual(gui.palette_for((0, 65535, 0))["track"], gui.LIGHT["track"])
        self.assertEqual(gui.palette_for((0, 0, 65535))["track"], gui.DARK["track"])

    def test_returns_a_copy(self):
        palette = gui.palette_for((0, 0, 0))
        palette["track"] = "#000000"
        self.assertNotEqual(gui.DARK["track"], "#000000")


class BarColorTest(unittest.TestCase):
    def test_thresholds_match_cli(self):
        palette = dict(gui.LIGHT)
        self.assertEqual(gui.bar_color(79.9, palette), palette["ok"])
        self.assertEqual(gui.bar_color(80, palette), palette["warn"])
        self.assertEqual(gui.bar_color(94.9, palette), palette["warn"])
        self.assertEqual(gui.bar_color(95, palette), palette["crit"])

    def test_agrees_with_cli_colour_function(self):
        palette = dict(gui.LIGHT)
        for percent in (0, 50, 79, 80, 90, 94, 95, 100, 130):
            cli = cu.color(percent)
            expected = {"": palette["ok"], cu.ESC_YELLOW: palette["warn"], cu.ESC_RED: palette["crit"]}[cli]
            self.assertEqual(gui.bar_color(percent, palette), expected, percent)


if __name__ == "__main__":
    unittest.main()
