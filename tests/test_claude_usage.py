# -*- coding: utf-8 -*-
"""claude_usage の単体テスト。ネットワークと Keychain には触れない。"""

import datetime
import email.message
import io
import re
import json
import traceback
import unittest
import urllib.error
from unittest import mock

import claude_usage as cu

FAKE_TOKEN = "sk-ant-oat-FAKE-TOKEN-VALUE-0123456789"

SAMPLE = {
    "five_hour": {"utilization": 34.0, "resets_at": "2026-09-15T09:40:00+00:00"},
    "seven_day": {"utilization": 32.0, "resets_at": "2026-09-19T17:00:00+00:00"},
    "seven_day_opus": None,
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 34,
            "severity": "normal",
            "resets_at": "2026-09-15T09:40:00+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 32,
            "resets_at": "2026-09-19T17:00:00+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 51,
            "resets_at": "2026-09-19T17:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True,
        },
    ],
}


def credentials(expires_at_ms=9999999999999, token=FAKE_TOKEN):
    oauth = {"expiresAt": expires_at_ms, "scopes": ["user:profile"]}
    if token is not None:
        oauth["accessToken"] = token
    return {"claudeAiOauth": oauth, "organizationUuid": "x"}


class ExtractWindowsTest(unittest.TestCase):
    def test_limits_is_the_source_of_truth(self):
        windows = cu.extract_windows(SAMPLE)
        self.assertEqual(
            [(w.label, w.percent, w.is_active) for w in windows],
            [("5時間枠", 34.0, False), ("週次 (全モデル)", 32.0, False), ("週次 (Fable)", 51.0, True)],
        )

    def test_weekly_scoped_without_scope(self):
        data = {"limits": [{"kind": "weekly_scoped", "percent": 10}]}
        self.assertEqual(cu.extract_windows(data)[0].label, "週次 (モデル別)")

    def test_unknown_kind_is_kept(self):
        data = {"limits": [{"kind": "monthly_future", "percent": 10}]}
        self.assertEqual(cu.extract_windows(data)[0].label, "monthly_future")

    def test_unknown_fields_are_ignored(self):
        data = json.loads(json.dumps(SAMPLE))
        data["brand_new_key"] = {"nested": [1, 2, 3]}
        data["limits"][0]["brand_new_field"] = {"a": 1}
        self.assertEqual(len(cu.extract_windows(data)), 3)

    def test_malformed_entries_are_skipped(self):
        data = {"limits": [None, "x", 3, {}, {"kind": "session", "percent": "nope"}, {"kind": "session", "percent": 5}]}
        windows = cu.extract_windows(data)
        self.assertEqual([(w.label, w.percent) for w in windows], [("5時間枠", 5.0)])

    def test_percent_falls_back_to_utilization(self):
        data = {"limits": [{"kind": "session", "utilization": 12.5}]}
        self.assertEqual(cu.extract_windows(data)[0].percent, 12.5)

    def test_bool_percent_is_rejected(self):
        data = {"limits": [{"kind": "session", "percent": True}]}
        self.assertEqual(cu.extract_windows(data), [])

    def test_fallback_when_limits_missing(self):
        data = {k: v for k, v in SAMPLE.items() if k != "limits"}
        self.assertEqual([w.label for w in cu.extract_windows(data)], ["5時間枠", "週次 (全モデル)"])

    def test_fallback_when_limits_empty(self):
        data = dict(SAMPLE, limits=[])
        self.assertEqual([w.label for w in cu.extract_windows(data)], ["5時間枠", "週次 (全モデル)"])

    def test_fallback_includes_known_and_unknown_model_keys(self):
        data = {
            "seven_day_opus": {"utilization": 5.0},
            "seven_day_cowork": {"utilization": 7.0},
            "seven_day_oauth_apps": None,
        }
        self.assertEqual([w.label for w in cu.extract_windows(data)], ["週次 (Opus)", "週次 (Cowork)"])

    def test_empty_and_invalid_input(self):
        self.assertEqual(cu.extract_windows({}), [])
        self.assertEqual(cu.extract_windows(None), [])
        self.assertEqual(cu.extract_windows([1, 2]), [])


class FmtRemainingTest(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 15, 7, 0, 0, tzinfo=datetime.timezone.utc)

    def test_days(self):
        self.assertEqual(cu.fmt_remaining("2026-09-19T17:00:00+00:00", self.NOW), "4d 10h")

    def test_hours(self):
        self.assertEqual(cu.fmt_remaining("2026-09-15T09:40:00+00:00", self.NOW), "2h 40m")

    def test_minutes(self):
        self.assertEqual(cu.fmt_remaining("2026-09-15T07:05:00+00:00", self.NOW), "5m")

    def test_past(self):
        self.assertEqual(cu.fmt_remaining("2026-09-15T06:00:00+00:00", self.NOW), "まもなく")

    def test_z_suffix(self):
        self.assertEqual(cu.fmt_remaining("2026-09-15T09:40:00Z", self.NOW), "2h 40m")

    def test_naive_is_treated_as_utc(self):
        self.assertEqual(cu.fmt_remaining("2026-09-15T09:40:00", self.NOW), "2h 40m")

    def test_microseconds(self):
        self.assertEqual(cu.fmt_remaining("2026-09-15T09:40:00.885868+00:00", self.NOW), "2h 40m")

    def test_missing_and_invalid(self):
        self.assertEqual(cu.fmt_remaining(None, self.NOW), "-")
        self.assertEqual(cu.fmt_remaining("", self.NOW), "-")
        self.assertEqual(cu.fmt_remaining("not-a-date", self.NOW), "-")
        self.assertEqual(cu.fmt_remaining(123, self.NOW), "-")


class ColorTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(cu.color(0), "")
        self.assertEqual(cu.color(79.9), "")
        self.assertEqual(cu.color(80), cu.ESC_YELLOW)
        self.assertEqual(cu.color(94.9), cu.ESC_YELLOW)
        self.assertEqual(cu.color(95), cu.ESC_RED)
        self.assertEqual(cu.color(140), cu.ESC_RED)


class BarTest(unittest.TestCase):
    def test_width_is_exact(self):
        for percent in (0, 1, 33.3, 50, 99, 100):
            self.assertEqual(len(cu.bar(percent, 20)), 20)

    def test_clamped(self):
        self.assertEqual(cu.bar(-10, 4), "░░░░")
        self.assertEqual(cu.bar(150, 4), "████")

    def test_zero_width(self):
        self.assertEqual(cu.bar(50, 0), "")


class BackoffTest(unittest.TestCase):
    def test_success_uses_interval(self):
        self.assertEqual(cu.backoff_delay(60, 0), 60)

    def test_exponential_growth(self):
        self.assertEqual(cu.backoff_delay(30, 1), 30)
        self.assertEqual(cu.backoff_delay(30, 2), 60)
        self.assertEqual(cu.backoff_delay(30, 3), 120)

    def test_capped(self):
        self.assertEqual(cu.backoff_delay(60, 20), cu.MAX_BACKOFF)

    def test_retry_after_wins_when_larger(self):
        self.assertEqual(cu.backoff_delay(30, 1, retry_after=120), 120)
        self.assertEqual(cu.backoff_delay(30, 1, retry_after=10), 30)

    def test_rate_limited_uses_longer_cap(self):
        self.assertEqual(cu.backoff_delay(300, 3, rate_limited=True), 1200)
        self.assertEqual(cu.backoff_delay(300, 20, rate_limited=True), cu.MAX_BACKOFF_RATE_LIMITED)
        self.assertEqual(cu.backoff_delay(300, 20), cu.MAX_BACKOFF)

    def test_never_shorter_than_interval(self):
        self.assertEqual(cu.backoff_delay(600, 5), 600)
        self.assertEqual(cu.backoff_delay(3600, 5, rate_limited=True), 3600)


class RetryAfterParseTest(unittest.TestCase):
    def _error(self, value):
        headers = email.message.Message()
        if value is not None:
            headers["Retry-After"] = value
        error = urllib.error.HTTPError("https://x", 429, "Too Many Requests", headers, io.BytesIO(b""))
        self.addCleanup(error.close)
        return error

    def test_seconds(self):
        self.assertEqual(cu._parse_retry_after(self._error("120")), 120)

    def test_absent_or_http_date(self):
        self.assertIsNone(cu._parse_retry_after(self._error(None)))
        self.assertIsNone(cu._parse_retry_after(self._error("Wed, 21 Oct 2026 07:28:00 GMT")))

    def test_clamped(self):
        self.assertEqual(cu._parse_retry_after(self._error("99999")), cu.MAX_RETRY_AFTER)


class TokenStoreTest(unittest.TestCase):
    def test_expiry_margin_boundary(self):
        expires_at_ms = 1_000_000_000_000
        with mock.patch.object(cu, "read_credentials", return_value=credentials(expires_at_ms)):
            store = cu.TokenStore()
        expires_sec = expires_at_ms / 1000.0
        self.assertFalse(store.is_expired(now=expires_sec - cu.EXPIRY_MARGIN_SEC - 1))
        self.assertTrue(store.is_expired(now=expires_sec - cu.EXPIRY_MARGIN_SEC))
        self.assertTrue(store.is_expired(now=expires_sec + 1))

    def test_missing_expiry_is_not_expired(self):
        creds = credentials()
        del creds["claudeAiOauth"]["expiresAt"]
        with mock.patch.object(cu, "read_credentials", return_value=creds):
            self.assertFalse(cu.TokenStore().is_expired())

    def test_missing_token(self):
        with mock.patch.object(cu, "read_credentials", return_value=credentials(token=None)):
            with self.assertRaises(cu.CredentialsError):
                cu.TokenStore()

    def test_missing_oauth_block(self):
        with mock.patch.object(cu, "read_credentials", return_value={"organizationUuid": "x"}):
            with self.assertRaises(cu.CredentialsError):
                cu.TokenStore()

    def test_keychain_failure(self):
        completed = mock.Mock(returncode=1, stdout=b"")
        with mock.patch.object(cu.platform, "system", return_value="Darwin"):
            with mock.patch.object(cu.subprocess, "run", return_value=completed):
                with self.assertRaises(cu.CredentialsError):
                    cu.read_credentials()

    def test_keychain_is_read_only(self):
        completed = mock.Mock(returncode=0, stdout=json.dumps(credentials()).encode())
        with mock.patch.object(cu.platform, "system", return_value="Darwin"):
            with mock.patch.object(cu.subprocess, "run", return_value=completed) as run:
                cu.read_credentials()
        self.assertEqual(run.call_args[0][0][:3], ["security", "find-generic-password", "-s"])
        self.assertIn("-w", run.call_args[0][0])


class HttpTest(unittest.TestCase):
    def test_only_anthropic_host_and_bearer_header(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(SAMPLE).encode()
        response.__enter__.return_value = response
        with mock.patch.object(cu.urllib.request, "urlopen", return_value=response) as urlopen:
            cu._http_get_usage(FAKE_TOKEN)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.host, "api.anthropic.com")
        self.assertEqual(list(request.headers.keys()), ["Authorization"])

    def test_invalid_json(self):
        response = mock.MagicMock()
        response.read.return_value = b"<html>"
        response.__enter__.return_value = response
        with mock.patch.object(cu.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(cu.UsageError):
                cu._http_get_usage(FAKE_TOKEN)

    def test_http_error_carries_status(self):
        error = urllib.error.HTTPError("https://x", 429, "Too Many", email.message.Message(), io.BytesIO(b""))
        self.addCleanup(error.close)
        with mock.patch.object(cu.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(cu.UsageError) as caught:
                cu._http_get_usage(FAKE_TOKEN)
        self.assertEqual(caught.exception.status, 429)

    def test_url_error(self):
        with mock.patch.object(cu.urllib.request, "urlopen", side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(cu.UsageError) as caught:
                cu._http_get_usage(FAKE_TOKEN)
        self.assertIsNone(caught.exception.status)


class NoTokenLeakTest(unittest.TestCase):
    """トークンが画面・例外に出ないことを保証する。"""

    def _assert_clean(self, text):
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("Bearer", text)

    def test_render_output(self):
        windows = cu.extract_windows(SAMPLE)
        lines = cu.render_lines(windows, datetime.datetime(2026, 9, 15, 16, 27, 43), "HTTP 500", 100)
        self._assert_clean("\n".join(lines))

    def test_exception_text_from_http_error(self):
        error = urllib.error.HTTPError("https://x", 401, "Unauthorized", email.message.Message(), io.BytesIO(b"body"))
        self.addCleanup(error.close)
        with mock.patch.object(cu.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(cu.UsageError) as caught:
                cu._http_get_usage(FAKE_TOKEN)
        exc = caught.exception
        self._assert_clean(str(exc))
        self._assert_clean(repr(exc))
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__)
        self._assert_clean("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def test_exception_text_from_generic_failure(self):
        with mock.patch.object(cu.urllib.request, "urlopen", side_effect=RuntimeError(FAKE_TOKEN)):
            with self.assertRaises(cu.UsageError) as caught:
                cu._http_get_usage(FAKE_TOKEN)
        exc = caught.exception
        self._assert_clean(str(exc))
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__)
        self._assert_clean("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def test_store_has_no_token_getter(self):
        with mock.patch.object(cu, "read_credentials", return_value=credentials()):
            store = cu.TokenStore()
        public = [name for name in dir(store) if not name.startswith("_")]
        self.assertEqual(sorted(public), ["fetch_usage", "is_expired", "load", "reload"])


class RenderTest(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 15, 7, 0, 0, tzinfo=datetime.timezone.utc)

    def test_fits_terminal_width(self):
        windows = cu.extract_windows(SAMPLE)
        for width in (40, 60, 80, 120, 200):
            for line in cu.render_lines(windows, None, None, width, self.NOW):
                plain = _strip_ansi(line)
                self.assertLessEqual(cu.display_width(plain), width, "width=%d line=%r" % (width, plain))

    def test_narrow_terminal_drops_bar(self):
        windows = cu.extract_windows(SAMPLE)
        text = _strip_ansi("\n".join(cu.render_lines(windows, None, None, 34, self.NOW)))
        self.assertNotIn("█", text)
        self.assertIn("34%", text)

    def test_active_marker_and_colors(self):
        windows = [cu.Window("週次 (Fable)", 96.0, None, True)]
        line = cu.render_lines(windows, None, None, 80, self.NOW)[2]
        self.assertIn(cu.ESC_RED, line)
        self.assertTrue(line.rstrip().endswith("←"))

    def test_long_label_is_truncated(self):
        windows = [cu.Window("x" * 60, 10.0, None, False)]
        line = _strip_ansi(cu.render_lines(windows, None, None, 80, self.NOW)[2])
        self.assertLessEqual(cu.display_width(line), 80)
        self.assertIn("…", line)

    def test_no_windows(self):
        text = _strip_ansi("\n".join(cu.render_lines([], None, "HTTP 500", 80)))
        self.assertIn("表示できる枠がありません", text)
        self.assertIn("取得失敗: HTTP 500", text)

    def test_status_line(self):
        text = _strip_ansi("\n".join(cu.render_lines([], None, None, 80)))
        self.assertIn("未取得", text)
        text = _strip_ansi("\n".join(cu.render_lines([], datetime.datetime(2026, 9, 15, 16, 27, 43), None, 80)))
        self.assertIn("最終更新 16:27:43", text)

    def test_draw_restores_nothing_stateful(self):
        stream = io.StringIO()
        cu.draw(stream, ["a", "b"])
        self.assertEqual(stream.getvalue(), cu.ESC_HOME + "a" + cu.ESC_CLEAR_LINE + "\n" + "b" + cu.ESC_CLEAR_LINE + "\n" + cu.ESC_CLEAR_BELOW)


class PollerTest(unittest.TestCase):
    def _store(self, expired=False, fetch=None):
        store = mock.Mock()
        store.is_expired.return_value = expired
        store.fetch_usage.side_effect = fetch
        return store

    def test_token_is_read_once_on_success(self):
        store = self._store(fetch=[SAMPLE, SAMPLE])
        poller = cu.Poller()
        poller._store = store
        poller.poll()
        poller.poll()
        self.assertEqual(store.reload.call_count, 0)

    def test_401_triggers_single_reload_then_succeeds(self):
        store = self._store(fetch=[cu.UsageError(401, "HTTP 401"), SAMPLE])
        poller = cu.Poller()
        poller._store = store
        self.assertEqual(poller.poll(), SAMPLE)
        self.assertEqual(store.reload.call_count, 1)

    def test_401_twice_reports_expired(self):
        store = self._store(fetch=[cu.UsageError(401, "HTTP 401"), cu.UsageError(401, "HTTP 401")])
        poller = cu.Poller()
        poller._store = store
        with self.assertRaises(cu.UsageError) as caught:
            poller.poll()
        self.assertEqual(str(caught.exception), cu.MSG_EXPIRED)

    def test_500_does_not_reload(self):
        store = self._store(fetch=[cu.UsageError(500, "HTTP 500")])
        poller = cu.Poller()
        poller._store = store
        with self.assertRaises(cu.UsageError):
            poller.poll()
        self.assertEqual(store.reload.call_count, 0)

    def test_expired_token_reloads_and_waits_without_calling_api(self):
        store = self._store(expired=True)
        poller = cu.Poller()
        poller._store = store
        with self.assertRaises(cu.UsageError) as caught:
            poller.poll()
        self.assertEqual(str(caught.exception), cu.MSG_EXPIRED)
        self.assertEqual(store.reload.call_count, 1)
        self.assertEqual(store.fetch_usage.call_count, 0)


class RunLoopTest(unittest.TestCase):
    def test_keeps_last_values_while_failing(self):
        poller = mock.Mock()
        poller.poll.side_effect = [SAMPLE, cu.UsageError(500, "HTTP 500"), StopIteration()]
        delays = []
        stream = io.StringIO()

        def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 2:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            cu.run(60, stream, poller=poller, sleep=sleep)

        text = _strip_ansi(stream.getvalue())
        self.assertIn("取得失敗: HTTP 500", text)
        self.assertIn("週次 (Fable)", text)
        self.assertEqual(delays, [60, 60])

    def test_credentials_error_invalidates_store(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.CredentialsError("読めません")
        stream = io.StringIO()

        def sleep(seconds):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            cu.run(60, stream, poller=poller, sleep=sleep)
        self.assertEqual(poller.invalidate.call_count, 1)
        self.assertIn("取得失敗: 読めません", _strip_ansi(stream.getvalue()))

    def test_retry_after_is_honoured(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.UsageError(429, "HTTP 429", retry_after=200)
        delays = []
        stream = io.StringIO()

        def sleep(seconds):
            delays.append(seconds)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            cu.run(60, stream, poller=poller, sleep=sleep)
        self.assertEqual(delays, [200])

    def test_rate_limit_backs_off_beyond_normal_cap(self):
        poller = mock.Mock()
        poller.poll.side_effect = cu.UsageError(429, "HTTP 429")
        delays = []
        stream = io.StringIO()

        def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 4:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            cu.run(300, stream, poller=poller, sleep=sleep)
        self.assertEqual(delays, [300, 600, 1200, 1800])


class CliTest(unittest.TestCase):
    def test_interval_lower_bound(self):
        captured = {}

        def fake_run(interval, stream, **kwargs):
            captured["interval"] = interval
            raise KeyboardInterrupt

        with mock.patch.object(cu, "run", fake_run):
            with mock.patch.object(cu.sys, "stdout", io.StringIO()):
                cu.main(["--interval", "5"])
        self.assertEqual(captured["interval"], cu.MIN_INTERVAL)

    def test_terminal_is_restored(self):
        stream = io.StringIO()
        with mock.patch.object(cu, "run", side_effect=KeyboardInterrupt):
            with mock.patch.object(cu.sys, "stdout", stream):
                self.assertEqual(cu.main([]), 0)
        output = stream.getvalue()
        self.assertTrue(output.startswith(cu.ESC_ALT_ON + cu.ESC_HIDE_CURSOR))
        self.assertTrue(output.endswith(cu.ESC_SHOW_CURSOR + cu.ESC_ALT_OFF + cu.ESC_RESET))

    def test_terminal_is_restored_on_sigterm(self):
        stream = io.StringIO()
        with mock.patch.object(cu, "run", side_effect=SystemExit(0)):
            with mock.patch.object(cu.sys, "stdout", stream):
                self.assertEqual(cu.main([]), 0)
        self.assertTrue(stream.getvalue().endswith(cu.ESC_SHOW_CURSOR + cu.ESC_ALT_OFF + cu.ESC_RESET))


_ANSI_RE = re.compile("\033\\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text):
    return _ANSI_RE.sub("", text)


if __name__ == "__main__":
    unittest.main()
