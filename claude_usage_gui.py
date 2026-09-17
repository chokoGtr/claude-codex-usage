#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude_usage の GUI 版。

取得・解釈は claude_usage をそのまま使い、描画だけを tkinter に差し替える。
トークンの扱いは CLI 版と同じで、この層はトークンに一切触れない。
"""

import argparse
import collections
import datetime
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

import claude_usage as cu

WINDOW_TITLE = "Claude Usage"
DEFAULT_WIDTH = 400
MIN_SIZE = (300, 150)
PAD_X = 18
PAD_TOP = 14
PAD_BOTTOM = 14
ROW_GAP = 14
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

Update = collections.namedtuple("Update", "windows error at")


# --- 取得ループ（tkinter に依存しない） -----------------------------------


def fetch_loop(poller, interval, publish, wait, should_stop):
    failures = 0
    while not should_stop():
        retry_after = None
        rate_limited = False
        try:
            publish(Update(cu.extract_windows(poller.poll()), None, datetime.datetime.now()))
            failures = 0
        except cu.CredentialsError as exc:
            poller.invalidate()
            publish(Update(None, str(exc), None))
            failures += 1
        except cu.UsageError as exc:
            publish(Update(None, str(exc), None))
            retry_after = exc.retry_after
            rate_limited = exc.status == 429
            failures += 1
        wait(cu.backoff_delay(interval, failures, retry_after, rate_limited))


# --- 配色 -----------------------------------------------------------------


def palette_for(background_rgb):
    """systemWindowBackgroundColor の輝度から明暗を判定する。"""
    red, green, blue = background_rgb
    luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 65535.0
    return dict(DARK if luma < 0.5 else LIGHT)


def bar_color(percent, palette):
    if percent >= 95:
        return palette["crit"]
    if percent >= 80:
        return palette["warn"]
    return palette["ok"]


# --- ウィンドウ -----------------------------------------------------------


class UsageWindow(object):
    def __init__(self, root, interval, topmost=False):
        self.root = root
        self.windows = []
        self.error = None
        self.last_ok_at = None
        self.row_count = None

        base = tkfont.nametofont("TkDefaultFont").actual("size")
        self.font_title = tkfont.Font(font="TkDefaultFont")
        self.font_title.configure(size=base + 3, weight="bold")
        self.font_label = tkfont.Font(font="TkDefaultFont")
        self.font_label.configure(size=base + 1)
        self.font_label_active = tkfont.Font(font="TkDefaultFont")
        self.font_label_active.configure(size=base + 1, weight="bold")
        self.font_value = tkfont.Font(font="TkFixedFont")
        self.font_value.configure(size=base + 1)
        self.font_small = tkfont.Font(font="TkDefaultFont")
        self.font_small.configure(size=max(base - 1, 8))

        root.title(WINDOW_TITLE)
        root.minsize(*MIN_SIZE)
        root.geometry("%dx%d" % (DEFAULT_WIDTH, MIN_SIZE[1]))
        if topmost:
            root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.close())

        self.canvas = tk.Canvas(root, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.redraw())

        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=fetch_loop,
            args=(cu.Poller(), interval, self.queue.put, self.stop_event.wait, self.stop_event.is_set),
            daemon=True,
        )
        self.thread.start()

        self.root.after(DRAIN_INTERVAL_MS, self._drain)
        self.root.after(TICK_INTERVAL_MS, self._tick)

    # 受信

    def _drain(self):
        received = False
        while True:
            try:
                update = self.queue.get_nowait()
            except queue.Empty:
                break
            received = True
            self.error = update.error
            if update.windows is not None:
                self.windows = update.windows
                self.last_ok_at = update.at
        if received:
            self.redraw()
        self.root.after(DRAIN_INTERVAL_MS, self._drain)

    def _tick(self):
        self.redraw()  # 残り時間の表示を進める
        self.root.after(TICK_INTERVAL_MS, self._tick)

    # 描画

    def _colors(self):
        try:
            palette = palette_for(self.root.winfo_rgb("systemWindowBackgroundColor"))
            palette["bg"] = "systemWindowBackgroundColor"
            palette["fg"] = "systemTextColor"
        except tk.TclError:
            palette = dict(LIGHT)
            palette["bg"] = "#ffffff"
            palette["fg"] = "#1d1d1f"
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

        width = canvas.winfo_width() or DEFAULT_WIDTH
        y = PAD_TOP
        canvas.create_text(PAD_X, y, text=WINDOW_TITLE, anchor="nw", fill=palette["fg"], font=self.font_title)
        y += self.font_title.metrics("linespace") + 12

        if not self.windows:
            message = "取得中…" if self.error is None else "表示できる枠がありません"
            canvas.create_text(PAD_X, y, text=message, anchor="nw", fill=palette["dim"], font=self.font_label)
            y += self.font_label.metrics("linespace") + ROW_GAP
        remaining = [cu.fmt_remaining(w.resets_at) for w in self.windows]
        column = max([self.font_small.measure(text) for text in remaining] + [0])
        for window, text in zip(self.windows, remaining):
            y = self._draw_row(canvas, window, y, width, palette, text, column)

        y += 2
        stamp = "未取得" if self.last_ok_at is None else "最終更新 " + self.last_ok_at.strftime("%H:%M:%S")
        canvas.create_text(PAD_X, y, text=stamp, anchor="nw", fill=palette["dim"], font=self.font_small)
        y += self.font_small.metrics("linespace")
        if self.error:
            y += 2
            canvas.create_text(
                PAD_X, y, text="取得失敗: " + self.error, anchor="nw",
                fill=palette["warn"], font=self.font_small, width=width - PAD_X * 2,
            )
            y += self.font_small.metrics("linespace")

        self._fit_height(len(self.windows), y + PAD_BOTTOM)

    def _draw_row(self, canvas, window, y, width, palette, remaining, column):
        label_font = self.font_label_active if window.is_active else self.font_label
        if window.is_active:
            center = y + label_font.metrics("linespace") / 2.0
            canvas.create_oval(
                PAD_X, center - DOT_RADIUS, PAD_X + DOT_RADIUS * 2, center + DOT_RADIUS,
                fill=palette["ok"], outline="",
            )
        canvas.create_text(
            PAD_X + LABEL_INDENT, y, text=window.label, anchor="nw", fill=palette["fg"], font=label_font
        )
        canvas.create_text(
            width - PAD_X, y, text="%d%%" % int(round(window.percent)), anchor="ne",
            fill=bar_color(window.percent, palette), font=self.font_value,
        )
        y += label_font.metrics("linespace") + 6

        bar_left = PAD_X + LABEL_INDENT
        bar_right = width - PAD_X - column - 10
        center = y + BAR_HEIGHT / 2.0
        if bar_right - bar_left >= BAR_HEIGHT:
            self._draw_bar(canvas, bar_left, bar_right, center, palette["track"])
            filled = bar_left + (bar_right - bar_left) * min(max(window.percent, 0.0), 100.0) / 100.0
            if filled > bar_left:
                self._draw_bar(canvas, bar_left, filled, center, bar_color(window.percent, palette))
        canvas.create_text(
            width - PAD_X, center, text=remaining, anchor="e", fill=palette["dim"], font=self.font_small
        )
        return y + max(BAR_HEIGHT, self.font_small.metrics("linespace")) + ROW_GAP

    def _draw_bar(self, canvas, left, right, center, color):
        radius = BAR_HEIGHT / 2.0
        canvas.create_line(
            left + radius, center, max(right - radius, left + radius), center,
            width=BAR_HEIGHT, capstyle=tk.ROUND, fill=color,
        )

    def _fit_height(self, row_count, height):
        if row_count == self.row_count:
            return
        self.row_count = row_count
        self.root.geometry("%dx%d" % (self.root.winfo_width() or DEFAULT_WIDTH, int(height)))

    def close(self):
        self.stop_event.set()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Claude のプラン使用量を GUI で常時表示する")
    parser.add_argument(
        "--interval",
        type=int,
        default=cu.DEFAULT_INTERVAL,
        help="ポーリング間隔（秒、下限 %d、既定 %d）" % (cu.MIN_INTERVAL, cu.DEFAULT_INTERVAL),
    )
    parser.add_argument("--topmost", action="store_true", help="ウィンドウを常に手前に表示する")
    args = parser.parse_args(argv)

    try:
        lock = cu.acquire_lock()
    except cu.LockError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1

    with lock:
        root = tk.Tk()
        UsageWindow(root, max(args.interval, cu.MIN_INTERVAL), topmost=args.topmost)
        root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
