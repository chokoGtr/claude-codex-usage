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
import getpass
import glob
import hashlib
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

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

API_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CONFIG_DIR = os.path.expanduser("~/.claude")
ENV_CONFIG_DIR = os.path.expanduser(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else None
CLAUDE_CONFIG_DIR = ENV_CONFIG_DIR or DEFAULT_CONFIG_DIR
KEYCHAIN_NOT_FOUND = 44  # security コマンドの errSecItemNotFound
LOCK_PATH = os.path.join(
    tempfile.gettempdir(),
    "claude-codex-usage-%s.lock" % (os.getuid() if hasattr(os, "getuid") else getpass.getuser()),
)

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
RESET_AT_WIDTH = 15

WEEKDAYS = "月火水木金土日"


Account = collections.namedtuple("Account", "name config_dir")


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


def read_credentials(config_dir=None):
    """認証情報 JSON を読み取り専用で取得する。config_dir 省略時は CLAUDE_CONFIG_DIR に従う。"""
    if config_dir is None:
        config_dir = ENV_CONFIG_DIR
    if platform.system() == "Darwin":
        raw = _read_keychain(keychain_services(config_dir))
    else:
        raw = _read_credentials_file(os.path.join(config_dir or DEFAULT_CONFIG_DIR, ".credentials.json"))
    try:
        data = json.loads(raw)
    except ValueError:
        raise CredentialsError("認証情報を解析できません") from None
    if not isinstance(data, dict):
        raise CredentialsError("認証情報を解析できません")
    return data


def keychain_services(config_dir):
    """Keychain の項目名の候補。

    Claude Code は CLAUDE_CONFIG_DIR を指定すると、そのパスの SHA-256 先頭 8 文字を付けた名前で保存する。
    パスは指定された文字列のまま使われるため、末尾の / の有無も候補に入れる。
    """
    if config_dir is None:
        return [KEYCHAIN_SERVICE]
    paths = [config_dir]
    trimmed = config_dir.rstrip("/")
    if trimmed and trimmed != config_dir:
        paths.append(trimmed)
    services = [
        "%s-%s" % (KEYCHAIN_SERVICE, hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]) for path in paths
    ]
    if trimmed == DEFAULT_CONFIG_DIR:
        services.insert(0, KEYCHAIN_SERVICE)
    return services


def _read_keychain(services):
    not_found = True
    for service in services:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except OSError:
            raise CredentialsError("security コマンドを実行できません") from None
        except subprocess.SubprocessError:
            raise CredentialsError("Keychain の読み取りがタイムアウトしました") from None
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace")
        if result.returncode != KEYCHAIN_NOT_FOUND:
            not_found = False
    if not_found:
        raise CredentialsError("Keychain に認証情報がありません（未ログインの可能性）")
    raise CredentialsError("Keychain から認証情報を読めません（ロック中の可能性）")


def _read_credentials_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        raise CredentialsError("%s を読めません" % path) from None


def account_name(config_dir):
    """~/.claude-work なら work。既定の ~/.claude は空文字。"""
    # WSL から渡す Windows 形式のパスも扱えるよう、区切りは / と \ の両方を見る
    base = config_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lstrip(".")
    if base.startswith("claude-"):
        base = base[len("claude-"):]
    return "" if base == "claude" else base


def parse_account(text):
    """`名前=ディレクトリ` または `ディレクトリ` を Account にする。名前を省くとディレクトリ名から付ける。"""
    name, sep, path = text.partition("=")
    if not sep:
        name, path = "", text
    path = os.path.expanduser(path.strip())
    if not path:
        raise argparse.ArgumentTypeError("ディレクトリを指定してください")
    return Account(name.strip() or account_name(path), path)


def account_title(account):
    return "Claude · %s" % account.name if account.name else "Claude Usage"


def has_credentials(config_dir):
    """ログイン済みかどうかだけを見る。macOS でも認証情報の中身は読まない。"""
    if platform.system() != "Darwin":
        return os.path.isfile(os.path.join(config_dir, ".credentials.json"))
    for service in keychain_services(config_dir):
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            return True
    return False


def discover_accounts():
    """CLAUDE_CONFIG_DIR 指定時はそれだけ。無ければ ~/.claude と ~/.claude-* のうちログイン済みのもの。"""
    if ENV_CONFIG_DIR is not None:
        return [Account("", None)]
    dirs = [DEFAULT_CONFIG_DIR] + sorted(
        path for path in glob.glob(glob.escape(DEFAULT_CONFIG_DIR) + "-*") if os.path.isdir(path)
    )
    accounts = [Account(account_name(path), path) for path in dirs if has_credentials(path)]
    return accounts or [Account("", None)]


class TokenStore(object):
    """アクセストークンを内部に閉じ込める。値を返す getter は持たない。"""

    def __init__(self, config_dir=None):
        self._config_dir = config_dir
        self._token = None
        self._expires_at_ms = None
        self.load()

    def load(self):
        oauth = read_credentials(self._config_dir).get("claudeAiOauth")
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


def fmt_reset_at(value, tz=None):
    parsed = parse_iso(value)
    if parsed is None:
        return "-"
    local = parsed.astimezone(tz)
    return "%d/%d(%s) %02d:%02d" % (local.month, local.day, WEEKDAYS[local.weekday()], local.hour, local.minute)


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


def render_lines(windows, last_ok_at, error, term_width, now=None, tz=None, title="Claude Usage"):
    """1 アカウント分。複数並べても端末に収まるよう、見出しと最終更新を 1 行にまとめる。"""
    stamp = "未取得" if last_ok_at is None else "最終更新 " + last_ok_at.strftime("%H:%M:%S")
    title = _truncate(title, max(term_width - display_width(stamp) - 4, 8))
    lines = [" " + ESC_BOLD + title + ESC_RESET + "  " + ESC_DIM + stamp + ESC_RESET]

    labels = [_truncate(w.label, MAX_LABEL_WIDTH) for w in windows]
    label_width = max([display_width(x) for x in labels] + [0])
    show_reset_at = term_width - label_width - 21 >= RESET_AT_WIDTH + 2
    bar_width = term_width - label_width - 21
    if show_reset_at:
        bar_width -= RESET_AT_WIDTH + 2
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
        if show_reset_at:
            line += "  " + _pad(fmt_reset_at(window.resets_at, tz), RESET_AT_WIDTH)
        if window.is_active:
            line += " ←"
        lines.append(line)

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

    def __init__(self, config_dir=None):
        self._config_dir = config_dir
        self._store = None

    def poll(self):
        if self._store is None:
            self._store = TokenStore(self._config_dir)
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


class AccountState(object):
    """1 アカウント分の表示値と、次に取得する時刻。失敗時の待ち時間はアカウントごとに伸ばす。"""

    def __init__(self, title, poller):
        self.title = title
        self.poller = poller
        self.windows = []
        self.last_ok_at = None
        self.error = None
        self.failures = 0
        self.due = 0.0

    def poll(self, interval, now):
        retry_after = None
        rate_limited = False
        try:
            self.windows = extract_windows(self.poller.poll())
            self.last_ok_at = datetime.datetime.now()
            self.error = None
            self.failures = 0
        except CredentialsError as exc:
            self.poller.invalidate()
            self.error = str(exc)
            self.failures += 1
        except UsageError as exc:
            self.error = str(exc)
            retry_after = exc.retry_after
            rate_limited = exc.status == 429
            self.failures += 1
        self.due = now + backoff_delay(interval, self.failures, retry_after, rate_limited)


def run(interval, stream, poller=None, sleep=time.sleep, accounts=None, clock=time.monotonic):
    if poller is not None:
        states = [AccountState("Claude Usage", poller)]
    else:
        states = [AccountState(account_title(a), Poller(a.config_dir)) for a in accounts or [Account("", None)]]

    while True:
        now = clock()
        for state in states:
            if state.due <= now:
                state.poll(interval, now)

        term_width = shutil.get_terminal_size((80, 24)).columns
        lines = []
        for state in states:
            if lines:
                lines.append("")
            lines += render_lines(state.windows, state.last_ok_at, state.error, term_width, title=state.title)
        draw(stream, lines)
        sleep(max(min(state.due for state in states) - now, 0))


def acquire_lock(path=None):
    """端末版と GUI 版で共有するロック。返したファイルを閉じるかプロセスが終わると解放される。"""
    try:
        handle = open(path or LOCK_PATH, "a")
    except OSError:
        raise LockError("ロックファイルを作れません") from None
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise LockError("claude-codex-usage は既に起動しています") from None
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
    parser.add_argument(
        "--claude",
        action="append",
        type=parse_account,
        metavar="[名前=]DIR",
        help="表示する Claude の設定ディレクトリ。複数指定可。省略時は ~/.claude と ~/.claude-* のログイン済みのもの",
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
            run(interval, stream, accounts=args.claude or discover_accounts())
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            stream.write(ESC_SHOW_CURSOR + ESC_ALT_OFF + ESC_RESET)
            stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
