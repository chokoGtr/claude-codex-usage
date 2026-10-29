#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude と Codex の使用量をまとめて表示する GUI 版。

取得・解釈は各サービスのモジュールに任せ、描画だけを tkinter で共通化する。
この層はトークンに一切触れない。
"""

import argparse
import collections
import datetime
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

import claude_usage as cu
import codex_usage as xu

WINDOW_TITLE = "Claude + Codex Usage"
DEFAULT_WIDTH = 400
MIN_SIZE = (300, 150)
PAD_X = 18
PAD_TOP = 14
PAD_BOTTOM = 6  # 最後の行の ROW_GAP に足す
ROW_GAP = 10
SECTION_GAP = 12
TITLE_GAP = 8
SCREEN_MARGIN = 120  # メニューバー・タイトルバー・Dock の分
SCROLL_STEP = 20
BAR_HEIGHT = 8
DOT_RADIUS = 3
LABEL_INDENT = 12
DRAIN_INTERVAL_MS = 200
TICK_INTERVAL_MS = 30000

LIGHT = {
    "dim": "#86868b",
    "track": "#e3e3e8",
    "ok": "#3a8c5f",
    "warn": "#b8860b",
    "crit": "#c9372c",
}
DARK = {
    "dim": "#8e8e93",
    "track": "#3a3a3c",
    "ok": "#4cc38a",
    "warn": "#e0a83d",
    "crit": "#f2665c",
}

WIN_LIGHT = {"bg": "#f3f3f3", "fg": "#1b1b1b"}
WIN_DARK = {"bg": "#202020", "fg": "#f3f3f3"}
WINDOWS_PYTHONS = ("pyw.exe", "pythonw.exe", "py.exe", "python.exe")

Update = collections.namedtuple("Update", "windows error at")


# --- 取得ループ（tkinter に依存しない） -----------------------------------


class FetchControl(object):
    """取得スレッドの待機を、手動更新または終了で解除する。"""

    def __init__(self):
        self._condition = threading.Condition()
        self._refresh_requested = False
        self._stopped = False

    def wait(self, seconds):
        with self._condition:
            self._condition.wait_for(
                lambda: self._refresh_requested or self._stopped,
                timeout=seconds,
            )
            self._refresh_requested = False

    def request_refresh(self):
        with self._condition:
            self._refresh_requested = True
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify()

    def is_stopped(self):
        with self._condition:
            return self._stopped


def fetch_loop(
    poller, interval, publish, wait, should_stop,
    extractor=cu.extract_windows, credentials_error=cu.CredentialsError,
    usage_error=cu.UsageError,
):
    failures = 0
    while not should_stop():
        retry_after = None
        rate_limited = False
        try:
            publish(Update(extractor(poller.poll()), None, datetime.datetime.now()))
            failures = 0
        except credentials_error as exc:
            poller.invalidate()
            publish(Update(None, str(exc), None))
            failures += 1
        except usage_error as exc:
            publish(Update(None, str(exc), None))
            retry_after = getattr(exc, "retry_after", None)
            rate_limited = getattr(exc, "status", None) == 429
            failures += 1
        wait(cu.backoff_delay(interval, failures, retry_after, rate_limited))


# --- 配色 -----------------------------------------------------------------


def palette_for(background_rgb):
    """systemWindowBackgroundColor の輝度から明暗を判定する。"""
    red, green, blue = background_rgb
    luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 65535.0
    return dict(DARK if luma < 0.5 else LIGHT)


def windows_dark_mode():
    """Windows のアプリ用テーマが暗色かどうか。読めなければ明色とみなす。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return False


def wheel_steps(delta):
    """マウスホイールの delta を行数にする。Tk 9 は 1 ノッチ 120、Tk 8.6 の macOS は小さな値で届く。"""
    if not delta:
        return 0
    value = delta / 120.0 if abs(delta) >= 120 else delta
    return -int(value) or (-1 if value > 0 else 1)


def touchpad_delta_y(delta):
    """<TouchpadScroll> の delta から縦方向のピクセル量を取り出す（tk::PreciseScrollDeltas と同じ）。"""
    low = delta & 0xFFFF
    return low if low < 0x8000 else low - 0x10000


def bar_color(percent, palette):
    if percent >= 95:
        return palette["crit"]
    if percent >= 80:
        return palette["warn"]
    return palette["ok"]


# --- ウィンドウ -----------------------------------------------------------


class UsageWindow(object):
    def __init__(self, root, interval, topmost=False, accounts=None):
        self.root = root
        accounts = accounts or [cu.Account("", None)]
        self.sources = collections.OrderedDict()
        for index, account in enumerate(accounts):
            self.sources["claude%d" % index] = self._source(cu.account_title(account))
        self.sources["codex"] = self._source("Codex Usage")
        self.row_count = None
        self.content_height = None
        self.windowing = root.tk.call("tk", "windowingsystem")
        self.dark = self.windowing == "win32" and windows_dark_mode()
        # 高 DPI の Windows では Tk のポイント指定だけが拡大されるため、図形も合わせる
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)

        base = tkfont.nametofont("TkDefaultFont").actual("size")
        if self.windowing == "win32":
            base = max(base, 10)  # 既定の 9pt は小さく見える
        self.font_title = tkfont.Font(font="TkDefaultFont")
        self.font_title.configure(size=base + 3, weight="bold")
        self.font_label = tkfont.Font(font="TkDefaultFont")
        self.font_label.configure(size=base + 1)
        self.font_label_active = tkfont.Font(font="TkDefaultFont")
        self.font_label_active.configure(size=base + 1, weight="bold")
        # Windows の TkFixedFont (Courier New) は浮くので本文と同じ書体にする
        self.font_value = tkfont.Font(font="TkDefaultFont" if self.windowing == "win32" else "TkFixedFont")
        self.font_value.configure(size=base + 1)
        self.font_small = tkfont.Font(font="TkDefaultFont")
        self.font_small.configure(size=max(base - 1, 8))

        root.title(WINDOW_TITLE)
        root.minsize(self.px(MIN_SIZE[0]), self.px(MIN_SIZE[1]))
        root.geometry("%dx%d" % (self.px(DEFAULT_WIDTH), self.px(MIN_SIZE[1])))
        if topmost:
            root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.close())

        self.canvas = tk.Canvas(
            root, highlightthickness=0, borderwidth=0, yscrollincrement=self.px(SCROLL_STEP),
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.redraw())
        # 画面に収まらないときだけ意味を持つ。収まっている間は scrollregion が窓より小さく動かない
        root.bind("<MouseWheel>", lambda event: self._scroll(wheel_steps(event.delta)))
        try:
            # Tk 8.7 以降の macOS ではトラックパッドは MouseWheel ではなくこちらで届く
            root.bind("<TouchpadScroll>", self._on_touchpad)
        except tk.TclError:
            pass
        root.bind("<Button-4>", lambda event: self._scroll(-1))
        root.bind("<Button-5>", lambda event: self._scroll(1))

        if self.windowing == "aqua":
            root.bind("<Command-r>", self.refresh)
        root.bind("<Control-r>", self.refresh)
        if self.dark:
            self._use_dark_title_bar()

        self.queue = queue.Queue()
        for index, account in enumerate(accounts):
            self._start_source(
                "claude%d" % index, cu.Poller(account.config_dir), interval,
                cu.extract_windows, cu.CredentialsError, cu.UsageError,
            )
        self._start_source("codex", xu.Poller(), interval, xu.extract_windows, (), xu.CodexUsageError)

        self.root.after(DRAIN_INTERVAL_MS, self._drain)
        self.root.after(TICK_INTERVAL_MS, self._tick)

    def px(self, value):
        return int(round(value * self.scale))

    def _use_dark_title_bar(self):
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（旧ビルドは 19）
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                ) == 0:
                    break
        except (AttributeError, OSError):
            pass

    # 受信

    @staticmethod
    def _source(title):
        return {
            "title": title, "windows": [], "error": None, "last_ok_at": None,
            "refreshing": True, "control": FetchControl(), "thread": None,
        }

    def _start_source(self, name, poller, interval, extractor, credentials_error, usage_error):
        source = self.sources[name]
        source["thread"] = threading.Thread(
            target=fetch_loop,
            args=(
                poller, interval, lambda update: self.queue.put((name, update)),
                source["control"].wait, source["control"].is_stopped,
                extractor, credentials_error, usage_error,
            ),
            daemon=True,
        )
        source["thread"].start()

    def close_on_stdin_eof(self):
        def watch():
            if sys.stdin is None:
                return
            # sys.stdin.buffer は終了時にロック待ちで落ちるため、fd を直接読む
            try:
                while os.read(sys.stdin.fileno(), 1024):
                    pass
            except (OSError, ValueError):
                pass
            self.queue.put((None, None))

        threading.Thread(target=watch, daemon=True).start()

    def _drain(self):
        received = False
        while True:
            try:
                name, update = self.queue.get_nowait()
            except queue.Empty:
                break
            if name is None:
                self.close()
                return
            received = True
            source = self.sources[name]
            source["error"] = update.error
            if update.windows is not None:
                source["windows"] = update.windows
                source["last_ok_at"] = update.at
            source["refreshing"] = False
        if received:
            self.redraw()
        self.root.after(DRAIN_INTERVAL_MS, self._drain)

    def _tick(self):
        self.redraw()  # 残り時間の表示を進める
        self.root.after(TICK_INTERVAL_MS, self._tick)

    def refresh(self, event=None):
        for name in self.sources:
            self.refresh_source(name)
        return "break"

    def refresh_source(self, name, event=None):
        source = self.sources[name]
        if source["refreshing"]:
            return "break"
        source["refreshing"] = True
        source["control"].request_refresh()
        self.redraw()
        return "break"

    # 描画

    def _colors(self):
        try:
            palette = palette_for(self.root.winfo_rgb("systemWindowBackgroundColor"))
            palette["bg"] = "systemWindowBackgroundColor"
            palette["fg"] = "systemTextColor"
        except tk.TclError:
            if self.dark:
                palette = dict(DARK)
                palette.update(WIN_DARK)
            else:
                palette = dict(LIGHT)
                palette.update(WIN_LIGHT)
        return palette

    def redraw(self):
        palette = self._colors()
        canvas = self.canvas
        canvas.delete("all")
        canvas.configure(background=palette["bg"])
        try:
            self.root.configure(background=palette["bg"])
        except tk.TclError:
            pass

        width = canvas.winfo_width() or self.px(DEFAULT_WIDTH)
        y = self._draw_refresh_all(canvas, self.px(PAD_TOP), width, palette)
        for index, (name, source) in enumerate(self.sources.items()):
            if index:
                y += self.px(SECTION_GAP)
            y = self._draw_source(canvas, name, source, y, width, palette)

        height = y + self.px(PAD_BOTTOM)
        canvas.configure(scrollregion=(0, 0, width, height))
        self.content_height = height
        signature = tuple(
            (len(s["windows"]), bool(s["error"])) for s in self.sources.values()
        )
        self._fit_height(signature, height)

    def _draw_refresh_all(self, canvas, y, width, palette):
        """右上に「↻ すべて更新」を置く。キーボードの更新と同じ動き。"""
        busy = all(source["refreshing"] for source in self.sources.values())
        shortcut = "⌘R" if self.windowing == "aqua" else "Ctrl+R"
        right = width - self.px(PAD_X)
        center = y + self.font_label.metrics("linespace") / 2.0
        hint = canvas.create_text(
            right, center, text=shortcut, anchor="e", fill=palette["dim"], font=self.font_small, tags=("refresh_all",),
        )
        canvas.create_text(
            canvas.bbox(hint)[0] - self.px(6), center, text="↻ すべて更新", anchor="e",
            fill=palette["dim"] if busy else palette["fg"], font=self.font_label, tags=("refresh_all",),
        )
        canvas.tag_bind("refresh_all", "<Button-1>", self.refresh)
        return y + self.font_label.metrics("linespace") + self.px(TITLE_GAP)

    def _draw_source(self, canvas, name, source, y, width, palette):
        canvas.create_text(
            self.px(PAD_X), y, text=source["title"],
            anchor="nw", fill=palette["fg"], font=self.font_title,
        )
        title_height = self.font_title.metrics("linespace")
        self._draw_stamp(canvas, name, source, y + title_height / 2.0, width, palette)
        y += title_height + self.px(TITLE_GAP)

        if not source["windows"]:
            message = "取得中…" if source["error"] is None else "表示できる枠がありません"
            canvas.create_text(self.px(PAD_X), y, text=message, anchor="nw", fill=palette["dim"], font=self.font_label)
            y += self.font_label.metrics("linespace") + self.px(ROW_GAP)
        remaining = ["%s · %s" % (cu.fmt_remaining(w.resets_at), cu.fmt_reset_at(w.resets_at)) for w in source["windows"]]
        column = max([self.font_small.measure(text) for text in remaining] + [0])
        for window, text in zip(source["windows"], remaining):
            y = self._draw_row(canvas, window, y, width, palette, text, column)

        if source["error"]:
            item = canvas.create_text(
                self.px(PAD_X), y, text="取得失敗: " + source["error"], anchor="nw",
                fill=palette["warn"], font=self.font_small, width=width - self.px(PAD_X) * 2,
            )
            y = canvas.bbox(item)[3] + self.px(ROW_GAP)

        return y

    def _draw_stamp(self, canvas, name, source, center, width, palette):
        """見出しの右に「↻ 最終更新 HH:MM:SS」を置く。どちらを押しても更新する。"""
        stamp_label = "最終読込 " if name == "codex" else "最終更新 "
        stamp = "未取得" if source["last_ok_at"] is None else stamp_label + source["last_ok_at"].strftime("%H:%M:%S")
        icon = "↻"
        icon_color = palette["dim"] if source["refreshing"] else palette["fg"]
        tag = "refresh_" + name
        right = width - self.px(PAD_X)
        canvas.create_text(right, center, text=stamp, anchor="e", fill=palette["dim"], font=self.font_small, tags=(tag,))
        canvas.create_text(
            right - self.font_small.measure(stamp) - self.px(6), center, text=icon, anchor="e",
            fill=icon_color, font=self.font_label, tags=(tag,),
        )
        canvas.tag_bind(tag, "<Button-1>", lambda event, key=name: self.refresh_source(key, event))

    def _draw_row(self, canvas, window, y, width, palette, remaining, column):
        label_font = self.font_label_active if window.is_active else self.font_label
        if window.is_active:
            center = y + label_font.metrics("linespace") / 2.0
            canvas.create_oval(
                self.px(PAD_X), center - self.px(DOT_RADIUS), self.px(PAD_X) + self.px(DOT_RADIUS) * 2, center + self.px(DOT_RADIUS),
                fill=palette["ok"], outline="",
            )
        canvas.create_text(
            self.px(PAD_X) + self.px(LABEL_INDENT), y, text=window.label, anchor="nw", fill=palette["fg"], font=label_font
        )
        canvas.create_text(
            width - self.px(PAD_X), y, text="%d%%" % int(round(window.percent)), anchor="ne",
            fill=bar_color(window.percent, palette), font=self.font_value,
        )
        y += label_font.metrics("linespace") + self.px(6)

        bar_left = self.px(PAD_X) + self.px(LABEL_INDENT)
        bar_right = width - self.px(PAD_X) - column - self.px(10)
        center = y + self.px(BAR_HEIGHT) / 2.0
        if bar_right - bar_left >= self.px(BAR_HEIGHT):
            self._draw_bar(canvas, bar_left, bar_right, center, palette["track"])
            filled = bar_left + (bar_right - bar_left) * min(max(window.percent, 0.0), 100.0) / 100.0
            if filled > bar_left:
                self._draw_bar(canvas, bar_left, filled, center, bar_color(window.percent, palette))
        canvas.create_text(
            width - self.px(PAD_X), center, text=remaining, anchor="e", fill=palette["dim"], font=self.font_small
        )
        return y + max(self.px(BAR_HEIGHT), self.font_small.metrics("linespace")) + self.px(ROW_GAP)

    def _draw_bar(self, canvas, left, right, center, color):
        radius = self.px(BAR_HEIGHT) / 2.0
        canvas.create_line(
            left + radius, center, max(right - radius, left + radius), center,
            width=self.px(BAR_HEIGHT), capstyle=tk.ROUND, fill=color,
        )

    def _fit_height(self, row_count, height):
        if row_count == self.row_count:
            return
        self.row_count = row_count
        limit = self.root.winfo_screenheight() - self.px(SCREEN_MARGIN)
        self.root.geometry("%dx%d" % (self.root.winfo_width() or self.px(DEFAULT_WIDTH), int(min(height, limit))))

    def _on_touchpad(self, event):
        dy = touchpad_delta_y(event.delta)
        if not dy or not self.content_height:
            return
        self.canvas.yview_moveto(self.canvas.yview()[0] - dy / float(self.content_height))

    def _scroll(self, steps):
        if steps:
            self.canvas.yview_scroll(steps, "units")

    def close(self):
        for source in self.sources.values():
            source["control"].stop()
        self.root.destroy()


def is_wsl():
    return platform.system() == "Linux" and "microsoft" in platform.release().lower()


def find_windows_python():
    """WSL から呼べる Windows の Python を探す。WSL 以外では None。"""
    if not is_wsl():
        return None
    for name in WINDOWS_PYTHONS:
        path = shutil.which(name)
        if path:
            return path
    return None


def to_windows_path(path):
    result = subprocess.run(
        ["wslpath", "-w", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    return result.stdout.decode("utf-8").strip()


def run_on_windows(python, args, accounts):
    """WSL の Tk は日本語や高 DPI の表示が崩れやすいので、Windows のネイティブ窓で表示する。

    認証情報とセッション記録は WSL 側のものを \\wsl.localhost 経由で読ませる。
    """
    env = dict(os.environ)
    env["CODEX_HOME"] = to_windows_path(xu.CODEX_HOME)
    shared = [name for name in env.get("WSLENV", "").split(":") if name]
    env["WSLENV"] = ":".join(shared + ["CODEX_HOME"])

    command = [python]
    if os.path.basename(python) in ("py.exe", "pyw.exe"):
        command.append("-3")
    command += [to_windows_path(os.path.abspath(__file__)), "--interval", str(args.interval), "--from-wsl"]
    if args.topmost:
        command.append("--topmost")
    for account in accounts:
        command += ["--claude", "%s=%s" % (account.name, to_windows_path(account.config_dir or cu.CLAUDE_CONFIG_DIR))]
    child = subprocess.Popen(command, env=env, stdin=subprocess.PIPE)
    try:
        return child.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        child.stdin.close()  # Windows 側の窓を閉じさせる


def enable_dpi_awareness():
    """Windows で Tk の描画がぼやけないようにする。他の OS では何もしない。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Claude と Codex のプラン使用量を GUI で常時表示する")
    parser.add_argument(
        "--interval",
        type=int,
        default=cu.DEFAULT_INTERVAL,
        help="ポーリング間隔（秒、下限 %d、既定 %d）" % (cu.MIN_INTERVAL, cu.DEFAULT_INTERVAL),
    )
    parser.add_argument(
        "--claude",
        action="append",
        type=cu.parse_account,
        metavar="[名前=]DIR",
        help="表示する Claude の設定ディレクトリ。複数指定可。省略時は ~/.claude と ~/.claude-* のログイン済みのもの",
    )
    parser.add_argument("--topmost", action="store_true", help="ウィンドウを常に手前に表示する")
    parser.add_argument("--wslg", action="store_true", help="WSL でも Windows の Python を使わず WSLg で表示する")
    # WSL 側から起動された Windows 側の窓。ロックは WSL 側が持ち、stdin が閉じたら終了する
    parser.add_argument("--from-wsl", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    accounts = args.claude or cu.discover_accounts()

    windows_python = None if args.wslg else find_windows_python()
    if args.from_wsl:
        lock = open(os.devnull)
    else:
        try:
            lock = cu.acquire_lock()
        except cu.LockError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 1

    with lock:
        if windows_python:
            return run_on_windows(windows_python, args, accounts)
        enable_dpi_awareness()
        root = tk.Tk()
        window = UsageWindow(root, max(args.interval, cu.MIN_INTERVAL), topmost=args.topmost, accounts=accounts)
        if args.from_wsl:
            window.close_on_stdin_eof()
        root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
