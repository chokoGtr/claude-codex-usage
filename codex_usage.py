#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex がローカルに保存した最新の使用量スナップショットを読む。"""

import datetime
import glob
import json
import os

import claude_usage as cu

CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
SESSIONS_GLOB = os.path.join(CODEX_HOME, "sessions", "**", "rollout-*.jsonl")


class CodexUsageError(Exception):
    """Codex のローカル使用量を読めなかった。"""


def _as_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _reset_iso(value):
    seconds = _as_number(value)
    if seconds is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            seconds, datetime.timezone.utc
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def extract_windows(rate_limits):
    """Codex の primary / secondary 枠を共通の Window へ変換する。"""
    if not isinstance(rate_limits, dict):
        return []
    windows = []
    for key in ("primary", "secondary"):
        block = rate_limits.get(key)
        if not isinstance(block, dict):
            continue
        percent = _as_number(block.get("used_percent"))
        minutes = _as_number(block.get("window_minutes"))
        if percent is None:
            continue
        if minutes == 300:
            label = "5時間枠"
        elif minutes == 10080:
            label = "週次"
        elif minutes is not None:
            label = "%d分枠" % int(minutes)
        else:
            label = "使用枠"
        windows.append(cu.Window(label, percent, _reset_iso(block.get("resets_at")), False))
    return windows


def _latest_rate_limits(path):
    latest = None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                if '"token_count"' not in line or '"rate_limits"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                value = payload.get("rate_limits")
                if extract_windows(value):
                    latest = value
    except OSError:
        return None
    return latest


def read_latest_usage(pattern=None):
    """更新日時の新しいセッションから有効なスナップショットを探す。"""
    paths = glob.glob(pattern or SESSIONS_GLOB, recursive=True)
    try:
        paths.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        pass
    for path in paths:
        value = _latest_rate_limits(path)
        if value is not None:
            return value
    if not paths:
        raise CodexUsageError("Codex のセッション記録がありません")
    raise CodexUsageError("Codex の使用量記録がありません")


class Poller(object):
    def poll(self):
        return read_latest_usage()

    def invalidate(self):
        pass
