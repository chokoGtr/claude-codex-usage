#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude のプラン使用量を端末に常時表示する。

非公式エンドポイント GET https://api.anthropic.com/api/oauth/usage を、
Claude Code が保存している OAuth アクセストークンで叩く。
トークンは読み取り専用で扱い、値は画面・ログ・例外のいずれにも出さない。
"""

import argparse
import collections
import datetime
import fcntl
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
LOCK_PATH = os.path.join(tempfile.gettempdir(), "claude-usage-%d.lock" % os.getuid())

HTTP_TIMEOUT = 20
MIN_INTERVAL = 120
DEFAULT_INTERVAL = 300
MAX_BACKOFF = 300
MAX_BACKOFF_RATE_LIMITED = 1800
MAX_RETRY_AFTER = 3600
EXPIRY_MARGIN_SEC = 60

MSG_EXPIRED = "アクセストークンが期限切れです。Claude Code を一度起動してください"

ESC_ALT_ON = "\033[?1049h"
ESC_ALT_OFF = "\033[?1049l"
ESC_HIDE_CURSOR = "\033[?25l"
ESC_SHOW_CURSOR = "\033[?25h"
ESC_HOME = "\033[H"
ESC_CLEAR_LINE = "\033[K"
ESC_CLEAR_BELOW = "\033[J"
ESC_RESET = "\033[0m"
ESC_BOLD = "\033[1m"
ESC_DIM = "\033[2m"
ESC_YELLOW = "\033[33m"
ESC_RED = "\033[31m"

MAX_LABEL_WIDTH = 20
MAX_BAR_WIDTH = 24
MIN_BAR_WIDTH = 8


class CredentialsError(Exception):
    """認証情報を読めなかった。メッセージにトークンは含めない。"""


class LockError(Exception):
    """別のインスタンスが起動している、またはロックファイルを作れない。"""


class UsageError(Exception):
    """使用量の取得に失敗した。メッセージにトークンは含めない。"""

    def __init__(self, status, message, retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.retry_after = retry_after

    def __str__(self):
        return self.message


# --- 認証情報 -------------------------------------------------------------


def read_credentials():
    """認証情報 JSON を読み取り専用で取得する。"""
    if platform.system() == "Darwin":
        raw = _read_keychain()
    else:
        raw = _read_credentials_file()
    try:
        data = json.loads(raw)
    except ValueError:
        raise CredentialsError("認証情報を解析できません") from None
    if not isinstance(data, dict):
        raise CredentialsError("認証情報を解析できません")
    return data


def _read_keychain():
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except OSError:
        raise CredentialsError("security コマンドを実行できません") from None
    except subprocess.SubprocessError:
        raise CredentialsError("Keychain の読み取りがタイムアウトしました") from None
    if result.returncode != 0:
        raise CredentialsError("Keychain から認証情報を読めません（ロック中の可能性）")
    return result.stdout.decode("utf-8", "replace")


def _read_credentials_file():
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        raise CredentialsError("~/.claude/.credentials.json を読めません") from None


class TokenStore(object):
    """アクセストークンを内部に閉じ込める。値を返す getter は持たない。"""

    def __init__(self):
        self._token = None
        self._expires_at_ms = None
        self.load()

    def load(self):
        oauth = read_credentials().get("claudeAiOauth")
        if not isinstance(oauth, dict):
            raise CredentialsError("認証情報に claudeAiOauth がありません")
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            raise CredentialsError("認証情報にアクセストークンがありません")
        expires = oauth.get("expiresAt")
        self._token = token
        self._expires_at_ms = expires if isinstance(expires, int) and not isinstance(expires, bool) else None

    def reload(self):
        self.load()

    def is_expired(self, now=None):
        if self._expires_at_ms is None:
            return False
        if now is None:
            now = time.time()
        return now >= (self._expires_at_ms / 1000.0) - EXPIRY_MARGIN_SEC

    def fetch_usage(self):
        return _http_get_usage(self._token)


# --- API ------------------------------------------------------------------


def _http_get_usage(token):
    request = urllib.request.Request(
        API_URL, headers={"Authorization": "Bearer " + token}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        retry_after = _parse_retry_after(exc)
        try:
            exc.close()  # HTTPError はソケットを握ったままになる
        except Exception:
            pass
        raise UsageError(status, "HTTP %d" % status, retry_after) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        raise UsageError(None, "接続できません (%s)" % type(reason).__name__) from None
    except Exception as exc:  # トークンを含みうる例外表現を外に出さない
        raise UsageError(None, "通信エラー (%s)" % type(exc).__name__) from None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UsageError(None, "応答を解析できません") from None
    if not isinstance(data, dict):
        raise UsageError(None, "応答を解析できません")
    return data


def _parse_retry_after(exc):
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None  # HTTP-date 形式は未確認のため扱わない
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER)


# --- レスポンスの解釈 -----------------------------------------------------

Window = collections.namedtuple("Window", "label percent resets_at is_active")

_KIND_LABELS = {
    "session": "5時間枠",
    "weekly_all": "週次 (全モデル)",
}

_FALLBACK_LABELS = {
    "five_hour": "5時間枠",
    "seven_day": "週次 (全モデル)",
    "seven_day_opus": "週次 (Opus)",
    "seven_day_sonnet": "週次 (Sonnet)",
}


def _as_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_text(value):
    return value if isinstance(value, str) and value else None


def _scope_label(scope):
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    if isinstance(model, dict):
        name = _as_text(model.get("display_name")) or _as_text(model.get("id"))
        if name:
            return name
    surface = scope.get("surface")
    if isinstance(surface, dict):
        name = _as_text(surface.get("display_name")) or _as_text(surface.get("id"))
        if name:
            return name
    return _as_text(scope.get("display_name"))


def extract_windows(data):
    """limits[] を正として枠の一覧を返す。無ければ旧形式の固定キーに落ちる。"""
    if not isinstance(data, dict):
        return []
    windows = []
    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            window = _window_from_limit(item)
            if window is not None:
                windows.append(window)
    if windows:
        return windows
    return _fallback_windows(data)


def _window_from_limit(item):
    if not isinstance(item, dict):
        return None
    percent = _as_number(item.get("percent"))
    if percent is None:
        percent = _as_number(item.get("utilization"))
    if percent is None:
        return None
    kind = _as_text(item.get("kind")) or ""
    if kind == "weekly_scoped":
        label = "週次 (%s)" % (_scope_label(item.get("scope")) or "モデル別")
    elif kind in _KIND_LABELS:
        label = _KIND_LABELS[kind]
    else:
        label = kind or "不明な枠"
    return Window(label, percent, _as_text(item.get("resets_at")), bool(item.get("is_active")))


def _fallback_windows(data):
    windows = []
    seen = set()
    for key, label in _FALLBACK_LABELS.items():
        window = _window_from_block(data.get(key), label)
        if window is not None:
            windows.append(window)
            seen.add(key)
    for key in sorted(data.keys()):
        if key in seen or not key.startswith("seven_day_"):
            continue
        suffix = key[len("seven_day_"):].replace("_", " ").title()
        window = _window_from_block(data.get(key), "週次 (%s)" % suffix)
        if window is not None:
            windows.append(window)
    return windows


def _window_from_block(block, label):
    if not isinstance(block, dict):
        return None
    percent = _as_number(block.get("utilization"))
    if percent is None:
        percent = _as_number(block.get("percent"))
    if percent is None:
        return None
    return Window(label, percent, _as_text(block.get("resets_at")), False)


# --- 表示 -----------------------------------------------------------------


def parse_iso(value):
    text = _as_text(value)
    if text is None:
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def fmt_remaining(value, now=None):
    parsed = parse_iso(value)
    if parsed is None:
        return "-"
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    seconds = int((parsed - now).total_seconds())
    if seconds <= 0:
        return "まもなく"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def color(percent):
    if percent >= 95:
        return ESC_RED
    if percent >= 80:
        return ESC_YELLOW
    return ""


def bar(percent, width):
    if width < 1:
        return ""
    value = min(max(percent, 0.0), 100.0)
    filled = min(int(round(width * value / 100.0)), width)
    return "█" * filled + "░" * (width - filled)


def _char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    return sum(_char_width(char) for char in text)


def _pad(text, width):
    padding = width - display_width(text)
    return text + " " * padding if padding > 0 else text


def _truncate(text, width):
    if display_width(text) <= width:
        return text
    result = ""
    used = 0
    for char in text:
        size = _char_width(char)
        if used + size > width - 1:
            break
        result += char
        used += size
    return result + "…"


def render_lines(windows, last_ok_at, error, term_width, now=None):
    lines = [" " + ESC_BOLD + "Claude Usage" + ESC_RESET, ""]

    labels = [_truncate(w.label, MAX_LABEL_WIDTH) for w in windows]
    label_width = max([display_width(x) for x in labels] + [0])
    bar_width = term_width - label_width - 21
    bar_width = min(bar_width, MAX_BAR_WIDTH)
    if bar_width < MIN_BAR_WIDTH:
        bar_width = 0

    if not windows:
        lines.append("  表示できる枠がありません")
    for label, window in zip(labels, windows):
        tone = color(window.percent)
        gauge = bar(window.percent, bar_width)
        body = "%s%3d%%%s" % (tone, int(round(window.percent)), ESC_RESET if tone else "")
        if gauge:
            body = "%s%s%s  %s" % (tone, gauge, ESC_RESET if tone else "", body)
        line = " %s  %s  %s" % (_pad(label, label_width), body, _pad(fmt_remaining(window.resets_at, now), 8))
        if window.is_active:
            line += " ←"
        lines.append(line)

    lines.append("")
    if last_ok_at is None:
        lines.append(" " + ESC_DIM + "未取得" + ESC_RESET)
    else:
        lines.append(" " + ESC_DIM + "最終更新 " + last_ok_at.strftime("%H:%M:%S") + ESC_RESET)
    if error:
        lines.append(" " + ESC_YELLOW + "取得失敗: " + error + "（表示は最終取得値）" + ESC_RESET)
    return lines


def draw(stream, lines):
    stream.write(ESC_HOME)
    for line in lines:
        stream.write(line + ESC_CLEAR_LINE + "\n")
    stream.write(ESC_CLEAR_BELOW)
    stream.flush()


# --- ポーリング -----------------------------------------------------------


def backoff_delay(interval, failures, retry_after=None, rate_limited=False):
    if failures <= 0:
        delay = interval
    else:
        cap = MAX_BACKOFF_RATE_LIMITED if rate_limited else MAX_BACKOFF
        delay = max(interval, min(interval * (2 ** min(failures - 1, 10)), cap))
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


class Poller(object):
    """トークンの再読み込み条件（401 と期限切れ）だけを扱う。"""

    def __init__(self):
        self._store = None

    def poll(self):
        if self._store is None:
            self._store = TokenStore()
        if self._store.is_expired():
            self._store.reload()
            if self._store.is_expired():
                raise UsageError(None, MSG_EXPIRED)
        try:
            return self._store.fetch_usage()
        except UsageError as exc:
            if exc.status != 401:
                raise
        self._store.reload()
        if self._store.is_expired():
            raise UsageError(None, MSG_EXPIRED)
        try:
            return self._store.fetch_usage()
        except UsageError as exc:
            if exc.status == 401:
                raise UsageError(401, MSG_EXPIRED) from None
            raise

    def invalidate(self):
        self._store = None


def run(interval, stream, poller=None, sleep=time.sleep):
    poller = poller or Poller()
    windows = []
    last_ok_at = None
    error = None
    failures = 0

    while True:
        retry_after = None
        rate_limited = False
        try:
            windows = extract_windows(poller.poll())
            last_ok_at = datetime.datetime.now()
            error = None
            failures = 0
        except CredentialsError as exc:
            poller.invalidate()
            error = str(exc)
            failures += 1
        except UsageError as exc:
            error = str(exc)
            retry_after = exc.retry_after
            rate_limited = exc.status == 429
            failures += 1

        term_width = shutil.get_terminal_size((80, 24)).columns
        draw(stream, render_lines(windows, last_ok_at, error, term_width))
        sleep(backoff_delay(interval, failures, retry_after, rate_limited))


def acquire_lock(path=None):
    """端末版と GUI 版で共有するロック。返したファイルを閉じるかプロセスが終わると解放される。"""
    try:
        handle = open(path or LOCK_PATH, "a")
    except OSError:
        raise LockError("ロックファイルを作れません") from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise LockError("claude_usage は既に起動しています") from None
    return handle


def _exit_on_signal(signum, frame):
    raise SystemExit(0)


def _install_signal_handlers():
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, _exit_on_signal)
            except (ValueError, OSError):
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Claude のプラン使用量を常時表示する")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help="ポーリング間隔（秒、下限 %d、既定 %d）" % (MIN_INTERVAL, DEFAULT_INTERVAL),
    )
    args = parser.parse_args(argv)
    interval = max(args.interval, MIN_INTERVAL)

    try:
        lock = acquire_lock()
    except LockError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1

    with lock:
        _install_signal_handlers()
        stream = sys.stdout
        stream.write(ESC_ALT_ON + ESC_HIDE_CURSOR)
        stream.flush()
        try:
            run(interval, stream)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            stream.write(ESC_SHOW_CURSOR + ESC_ALT_OFF + ESC_RESET)
            stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
