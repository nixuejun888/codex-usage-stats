#!/usr/bin/env python3
"""Codex usage widget, docked inside the Codex client window.

Shows today's tokens, the cache-hit rate, the live session and a 7-day chart.
The panel is owned by the Codex window, so it rides above it and hides with it,
and every position is clamped inside the client rect — it cannot be dragged out
of the app window. Money is in CNY from DeepSeek's own yuan price list (`--usd`
switches to the dollar list). The panel is deliberately locked: there is no
close button and WM_CLOSE is refused, so `hooks/ensure_widget.py --stop` is the
only way to shut it down. Numbers refresh from an append-only tail of the newest
rollout log (~1s), re-based by a full scan every `--rescan` seconds.
Standard library only; read-only on the logs.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_usage  # noqa: E402  (sibling module, imported after sys.path fix)
import deepseek_pricing  # noqa: E402  (sibling module, imported after sys.path fix)

user32 = ctypes.windll.user32

BG = "#12161c"
BG_SOFT = "#1b212b"
FG = "#e8edf4"
FG_DIM = "#8b98a9"
ACCENT = "#4cc2a0"
BAR = "#478d78"
BAR_EMPTY = "#26303c"
BAR_TODAY = ACCENT
TRACK = "#232c38"
RAW = 14  # vertical room reserved for the "近 7 天" caption inside the canvas

GWL_HWNDPARENT = -8
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
WIDGET_MUTEX_NAME = r"Local\CodexUsageStatsWidget"
STOP_EVENT_NAME = r"Local\CodexUsageStatsStop"
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")


# --------------------------------------------------------------------- win32
def claim_single_instance() -> bool:
    """True when this process is the only panel running on the machine.

    The mutex handle is deliberately never closed: it is released by the kernel
    when this process exits, which is exactly the lifetime we want. Callers that
    do not open a window (`--selftest`, `--watch`) skip this so they can run
    alongside a live panel.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    handle = kernel32.CreateMutexW(None, False, WIDGET_MUTEX_NAME)
    if not handle:
        return True  # cannot tell; never block the panel on a win32 hiccup
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def create_stop_event() -> int:
    """Named event that the only sanctioned way to close the panel sets.

    The panel has no close button and refuses WM_CLOSE, so shutting it down on
    purpose needs a channel that is not a click: `ensure_widget.py --stop`
    opens this event and sets it, and the panel notices within a second.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = wt.HANDLE
    return kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME) or 0


def stop_requested(handle: int) -> bool:
    """True once `ensure_widget.py --stop` has signalled this event."""
    if not handle:
        return False
    return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0


def process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    buffer = ctypes.create_unicode_buffer(1024)
    size = wt.DWORD(1024)
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
    kernel32.CloseHandle(handle)
    return os.path.basename(buffer.value) if ok else ""


def window_process(hwnd: int) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return process_name(pid.value)


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def toplevel_hwnd(root: tk.Misc) -> int:
    """Tk on Windows hands out the inner `TkChild` window from `winfo_id()`.

    The window that owns the frame (and the one that takes an owner window) is
    its parent, the `TkTopLevel` wrapper. Setting GWL_HWNDPARENT on the child
    would reparent the widget into the client instead of making it owned.
    """
    inner = int(root.winfo_id())
    outer = int(user32.GetParent(inner))
    if outer and window_class(outer).startswith("TkTopLevel"):
        return outer
    if window_class(inner).startswith("TkTopLevel"):
        return inner
    return outer or inner


def client_area_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Screen rect of a window's visible content, so the panel sits inside the app."""
    box = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(box)):
        return window_rect(hwnd)
    width, height = box.right - box.left, box.bottom - box.top
    if width < 240 or height < 240 or user32.IsZoomed(hwnd):
        return window_rect(hwnd)
    origin = wt.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return window_rect(hwnd)
    return origin.x, origin.y, origin.x + width, origin.y + height


def client_candidates(exe_names: tuple[str, ...]) -> list[tuple[int, tuple[int, int, int, int]]]:
    """Visible, non-minimised top-level windows belonging to the given processes."""
    wanted = {name.lower() for name in exe_names}
    found: list[tuple[int, tuple[int, int, int, int], int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        rect = client_area_rect(hwnd)
        if rect is None:
            return True
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area < 40_000:  # skip notify hosts and tool windows
            return True
        if window_process(hwnd).lower() not in wanted:
            return True
        found.append((int(hwnd), rect, area))
        return True

    user32.EnumWindows(callback, 0)
    found.sort(key=lambda item: item[2], reverse=True)
    return [(hwnd, rect) for hwnd, rect, _area in found]


def pick_client(exe_names: tuple[str, ...]) -> tuple[int | None, tuple[int, int, int, int] | None]:
    candidates = client_candidates(exe_names)
    if not candidates:
        return None, None
    foreground = int(user32.GetForegroundWindow())
    for hwnd, rect in candidates:
        if hwnd == foreground:
            return hwnd, rect
    return candidates[0]


def set_owner(widget_hwnd: int, owner_hwnd: int) -> None:
    """Owned windows stay above their owner and hide when it is minimised."""
    getter = getattr(user32, "GetWindowLongPtrW", None)
    setter = getattr(user32, "SetWindowLongPtrW", None)
    if getter is None or setter is None:
        getter, setter = user32.GetWindowLongW, user32.SetWindowLongW
    if getter(widget_hwnd, GWL_HWNDPARENT) != owner_hwnd:
        setter(widget_hwnd, GWL_HWNDPARENT, owner_hwnd)


def work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


# -------------------------------------------------------------------- helpers
def human(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.2f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def money_text(value: float | None, currency: str = deepseek_pricing.DEFAULT_CURRENCY) -> str:
    """`¥0.53` (default) or `$0.53` with --usd."""
    return deepseek_pricing.money(value, currency)


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                # line buffering keeps --watch readable when stdout is a pipe
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (OSError, ValueError):
                pass


def zero_usage() -> dict:
    return dict.fromkeys(USAGE_KEYS, 0)


def add_usage(target: dict, usage: dict) -> None:
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int):
            target[key] = target.get(key, 0) + value


def thread_of(path: Path | None) -> str:
    """Rollout filenames end in the thread uuid: rollout-<stamp>-<uuid>.jsonl."""
    return path.stem[-36:] if path else ""


def newest_rollout(codex_home: Path) -> Path | None:
    """The live log, judged by the timestamp of the last record it holds.

    File metadata cannot answer this on Windows: while another process holds a
    log open, the directory entry reports a stale mtime (`st_size` happens to
    refresh, `st_mtime` does not), so a log being written right now can look
    hours idle. The tail of each log is authoritative, and only the last few
    KB are read.
    """
    best: tuple[str, Path] | None = None
    for path in candidate_logs(codex_home):
        stamp = last_record_time(path)
        if stamp and (best is None or stamp > best[0]):
            best = (stamp, path)
    return best[1] if best else None


def candidate_logs(codex_home: Path, limit: int = 40) -> list[Path]:
    """Logs worth watching: the newest by path, which is date-ordered by folder."""
    try:
        files = sorted(codex_usage.rollout_files(codex_home, True), reverse=True)
    except OSError:
        return []
    return files[:limit]


def last_record_time(path: Path, window: int = 16_384) -> str:
    """Timestamp string of the last well-formed record, or '' when unreadable."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(size - window, 0))
            chunk = handle.read()
    except OSError:
        return ""
    for line in reversed(chunk.split(b"\n")):
        if b'"timestamp"' not in line:
            continue
        try:
            stamp = json.loads(line).get("timestamp")
        except json.JSONDecodeError:
            continue
        if isinstance(stamp, str) and stamp:
            return stamp
    return ""


class LiveTail:
    """Append-only reader over every rollout log that is still growing."""

    def __init__(self, codex_home: Path, tracked: int = 40, pricing: bool = True,
                 currency: str = deepseek_pricing.DEFAULT_CURRENCY) -> None:
        self.codex_home = codex_home
        self.tracked = tracked
        self.pricing = pricing
        self.currency = currency
        self.offsets: dict[Path, int] = {}
        self.pending: dict[Path, bytes] = {}
        self.seen: set[str] = set()
        self.active: Path | None = None
        # A tail only sees bytes written after its rebase, so the turn_context of
        # the turn already in flight is behind the offset: default_model carries
        # the session's model in from the last full scan.
        self.models: dict[str, str] = {}
        self.default_model = ""
        self._listing: list[Path] = []
        self._listed_at = 0.0

    def _size(self, path: Path) -> int | None:
        """Live byte count. Handle/stat refreshes what enumeration leaves stale."""
        try:
            return path.stat().st_size
        except OSError:  # rotated away between polls
            return None

    def _candidates(self) -> list[Path]:
        """Watch the newest logs; a fresh listing every 15s keeps this cheap."""
        if time.monotonic() - self._listed_at < 15:
            return self._listing
        self._listing = candidate_logs(self.codex_home, self.tracked)
        self._listed_at = time.monotonic()
        for path in self._listing:
            size = self._size(path)
            if size is not None:  # a log we have never seen is the scan's business
                self.offsets.setdefault(path, size)
        return self._listing

    @property
    def active_thread(self) -> str:
        return thread_of(self.active)

    def rebase(self, newest: Path | None) -> None:
        """Adopt a finished scan: only bytes written from now on are counted."""
        self.offsets = {}
        self.pending = {}
        self.seen.clear()
        self.active = newest
        self._listed_at = 0.0
        for path in self._candidates():
            size = self._size(path)
            if size is not None:
                self.offsets[path] = size

    def poll(self) -> dict | None:
        """Newly counted usage, or {"reset": True} when a log was rewritten."""
        for path in self._candidates():
            size = self._size(path)
            if size is None:
                continue
            if path not in self.offsets:
                self.offsets[path] = size  # new log: its history belongs to the scan
                continue
            offset = self.offsets[path]
            if size < offset:
                return {"reset": True}
            if size == offset:
                continue
            delta, counted = self._read(path, offset, size)
            self.offsets[path] = size
            if counted:
                self.active = path
                delta["thread"] = thread_of(path)
                return delta
        return None

    def _read(self, path: Path, offset: int, size: int) -> tuple[dict, int]:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)
        lines = (self.pending.pop(path, b"") + chunk).split(b"\n")
        self.pending[path] = lines.pop()

        delta = zero_usage()
        delta["cost"] = 0.0
        counted = 0
        for line in lines:
            if b'"token_usage_record"' not in line and b'"turn_context"' not in line:
                continue
            try:
                event = json.loads(line)
                payload = event.get("payload") or {}
            except (json.JSONDecodeError, AttributeError):
                continue
            if b'"turn_context"' in line:
                turn_id, model = payload.get("turn_id"), payload.get("model")
                if turn_id and model:
                    if len(self.models) > 2000:  # never let this grow without bound
                        self.models.clear()
                    self.models[str(turn_id)] = str(model)
                    self.default_model = str(model)
                continue
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                continue
            key = str(payload.get("response_id") or "")
            if key and key in self.seen:
                continue
            if key:
                self.seen.add(key)
            add_usage(delta, usage)
            if self.pricing:
                model = self.models.get(str(payload.get("turn_id") or "")) or self.default_model
                delta["cost"] += deepseek_pricing.cost_of(
                    usage, model, event.get("timestamp"), currency=self.currency) or 0.0
            counted += 1
        return delta, counted


# --------------------------------------------------------------------- widget
class UsageWidget:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.codex_home = Path(options.codex_home)
        self.pricing = not getattr(options, "no_price", False)
        self.currency = "USD" if getattr(options, "usd", False) \
            else deepseek_pricing.DEFAULT_CURRENCY
        self.locked = not getattr(options, "allow_close", False)
        self.results: queue.Queue = queue.Queue()
        self.scanning = False
        self.tail = LiveTail(self.codex_home, pricing=self.pricing, currency=self.currency)
        self.stop_event = create_stop_event()

        self.client: int | None = None
        self.client_rect: tuple[int, int, int, int] | None = None
        self.offset_from_corner = (options.margin, options.margin)
        self.drag_origin: tuple[int, int] | None = None
        self.headless = bool(getattr(options, "watch", 0))
        self.ticks = 0

        self.today = zero_usage()
        self.today_cost = 0.0
        self.session_total = 0
        self.session_cost = 0.0
        self.thread_totals: dict[str, int] = {}
        self.thread_costs: dict[str, float] = {}
        self.log_totals: dict[str, int] = {}
        self.live_threads: dict[str, int] = {}
        self.live_costs: dict[str, float] = {}
        self.active_thread = ""
        self.model = ""
        self.window7: list[tuple[str, int]] = []
        self.leader = ""
        self.files = 0

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", not options.no_topmost)
        self.root.attributes("-alpha", options.opacity)
        self.root.configure(bg=BG)
        self.root.title("Codex usage")

        self._build()
        self.root.update_idletasks()
        if self.locked:
            # No close button exists; this also swallows Alt+F4 and any WM_CLOSE,
            # so the panel cannot be dismissed by accident. The sanctioned way out
            # is `pythonw hooks/ensure_widget.py --stop`.
            self.root.protocol("WM_DELETE_WINDOW", self._refuse_close)
        self.hwnd = toplevel_hwnd(self.root)
        self.width = max(self.root.winfo_reqwidth(), self.root.winfo_width())
        self.height = max(self.root.winfo_reqheight(), self.root.winfo_height())
        self._attach_client(initial=True)
        self.refresh_scan()

        self.root.after(options.follow_ms, self._follow)
        self.root.after(1000, self._live_tick)
        self.root.after(200, self._drain)
        self.root.after(options.rescan * 1000, self._scan_tick)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = tk.Frame(self.root, bg=BG, padx=12, pady=9)
        pad.pack(fill="both", expand=True)

        header = tk.Frame(pad, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="CODEX 用量", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        lock = tk.Label(header, text="🔒 已锁定" if self.locked else "",
                        bg=BG, fg=FG_DIM, font=("Segoe UI", 7))
        lock.pack(side="right")
        self.dock_state = tk.Label(header, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 7))
        self.dock_state.pack(side="right", padx=(0, 6))

        total_row = tk.Frame(pad, bg=BG)
        total_row.pack(fill="x", pady=(2, 0))
        self.total = tk.Label(total_row, text="…", bg=BG, fg=FG,
                              font=("Segoe UI", 22, "bold"), anchor="w")
        self.total.pack(side="left")
        self.total_cost = tk.Label(total_row, text="", bg=BG, fg=ACCENT,
                                   font=("Segoe UI", 11, "bold"), anchor="e")
        self.total_cost.pack(side="right", pady=(8, 0))

        hit_row = tk.Frame(pad, bg=BG)
        hit_row.pack(fill="x")
        self.hit = tk.Label(hit_row, text="", bg=BG, fg=ACCENT,
                            font=("Segoe UI", 9, "bold"), anchor="w")
        self.hit.pack(side="left")
        self.hit_detail = tk.Label(hit_row, text="", bg=BG, fg=FG_DIM,
                                   font=("Segoe UI", 7), anchor="e")
        self.hit_detail.pack(side="right")
        self.hit_bar = tk.Canvas(pad, width=206, height=5, bg=TRACK, highlightthickness=0)
        self.hit_bar.pack(fill="x", pady=(1, 3))

        self.split = tk.Label(pad, text="", bg=BG, fg=FG_DIM,
                              font=("Consolas", 8), anchor="w")
        self.split.pack(fill="x")
        self.session = tk.Label(pad, text="", bg=BG, fg=FG_DIM,
                                font=("Segoe UI", 8), anchor="w")
        self.session.pack(fill="x", pady=(1, 0))
        self.top_thread = tk.Label(pad, text="", bg=BG, fg=FG_DIM,
                                   font=("Segoe UI", 8), anchor="w")
        self.top_thread.pack(fill="x", pady=(0, 2))

        self.spark = tk.Canvas(pad, width=206, height=42, bg=BG_SOFT, highlightthickness=0)
        self.spark.pack(fill="x", pady=(2, 3))
        self.footer = tk.Label(pad, text="", bg=BG, fg=FG_DIM,
                               font=("Segoe UI", 7), anchor="w")
        self.footer.pack(fill="x")

        for widget in (pad, header, total_row, self.total, self.total_cost,
                       self.split, self.session,
                       self.top_thread, self.spark, self.footer, self.hit, hit_row):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._menu)
            widget.bind("<Double-Button-1>", lambda _event: self.refresh_scan())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="立即重算", command=self.refresh_scan)
        self.menu.add_command(label="贴回客户端右下角", command=self._redock)
        self.menu.add_command(label="置顶开关", command=self._toggle_topmost)
        self.menu.add_command(label="价目说明", command=self._show_pricing)
        self.menu.add_separator()
        if self.locked:
            self.menu.add_command(label="已锁定：不可关闭", state="disabled")
        else:
            self.menu.add_command(label="退出挂件", command=self.root.destroy)

    def _refuse_close(self) -> None:
        """Swallow a close request; the panel is meant to stay docked.

        Leaving a visible trace matters here: a silently ignored Alt+F4 looks
        like a broken window rather than a deliberate lock.
        """
        self.footer.configure(text="已锁定，不可关闭")
        self.root.after(6000, self._render_numbers)

    def _show_pricing(self) -> None:
        """Show the exact basis of the money figure, so it can be checked."""
        if not self.pricing:
            messagebox.showinfo("价目", "本次以 --no-price 启动，只显示 token，不显示费用。")
            return
        model = deepseek_pricing.resolve(self.model) or f"{self.model or '未知'}（按 Flash 价估算）"
        messagebox.showinfo(
            "价目来源",
            f"DeepSeek 官方价目（{self.currency} / 每 1M token）\n\n"
            f"{deepseek_pricing.describe(self.currency)}\n\n"
            "高峰时段：北京时间周一至周五 09:00-12:00 与 14:00-18:00"
            "（UTC 01:00-04:00、06:00-10:00）；\n"
            "空闲时段为高峰价的一半，周末与中国法定节假日全天按空闲计。\n"
            "每条 response 按自己的时间戳定价，不按日汇总。\n\n"
            f"当前会话模型：{model}\n"
            f"今日费用：{money_text(self.today_cost, self.currency)}\n"
            f"本次会话：{money_text(self.session_cost, self.currency)}\n\n"
            "这是按官方标价算的估算，不是账单；余额、赠金与账号折扣看不到。",
        )

    # --------------------------------------------------------------- docking
    def _attach_client(self, initial: bool = False) -> bool:
        if self.client and user32.IsWindow(self.client) and client_area_rect(self.client):
            self.client_rect = client_area_rect(self.client)
            return True
        hwnd, rect = pick_client(tuple(self.options.client_exe))
        self.client, self.client_rect = hwnd, rect
        if hwnd and initial:
            set_owner(self.hwnd, hwnd)
        return bool(hwnd)

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        rect = self.client_rect or work_area(self.root)
        left, top, right, bottom = rect
        x = max(left, min(x, right - self.width))
        y = max(top, min(y, bottom - self.height))
        return x, y

    def _apply_position(self) -> None:
        rect = self.client_rect
        if rect is None:
            return
        offset_x, offset_y = self.offset_from_corner
        x, y = self._clamp(rect[2] - self.width - offset_x,
                           rect[3] - self.height - offset_y)
        if (x, y) != (self.root.winfo_x(), self.root.winfo_y()):
            self.root.geometry(f"+{x}+{y}")

    def _redock(self) -> None:
        self.offset_from_corner = (self.options.margin, self.options.margin)
        self._attach_client()
        self._apply_position()

    def _visible_client(self) -> bool:
        if not self.client or not user32.IsWindow(self.client):
            return False
        if user32.IsIconic(self.client) or not user32.IsWindowVisible(self.client):
            return False
        if self.options.stay_visible:
            return True
        foreground = int(user32.GetForegroundWindow())
        if foreground == self.client:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        if pid.value == os.getpid():  # the widget itself has focus
            return True
        return window_process(foreground).lower() == window_process(self.client).lower()

    def _follow(self) -> None:
        try:
            self._attach_client()
            if self.headless:
                pass
            elif not self._visible_client():
                if self.root.state() != "withdrawn":
                    self.root.withdraw()
            else:
                if self.root.state() == "withdrawn":
                    self.root.deiconify()
                set_owner(self.hwnd, self.client)
                self._apply_position()
        finally:
            self.root.after(self.options.follow_ms, self._follow)

    # ------------------------------------------------------------ interactions
    def _drag_start(self, event) -> None:
        self.drag_origin = (event.x_root - self.root.winfo_x(),
                            event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if self.drag_origin is None:
            return
        x, y = self._clamp(event.x_root - self.drag_origin[0],
                           event.y_root - self.drag_origin[1])
        self.root.geometry(f"+{x}+{y}")
        rect = self.client_rect
        if rect:
            self.offset_from_corner = (max(rect[2] - x - self.width, 0),
                                       max(rect[3] - y - self.height, 0))

    def _drag_end(self, _event) -> None:
        self.drag_origin = None

    def _menu(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", not self.root.attributes("-topmost"))

    # ------------------------------------------------------------------ data
    def _scan_tick(self) -> None:
        self.refresh_scan()
        self.root.after(self.options.rescan * 1000, self._scan_tick)

    def refresh_scan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.footer.configure(text="重算中…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            rows, stats = codex_usage.scan(self.codex_home, None, None,
                                          pricing=self.pricing, currency=self.currency)
            by_day = codex_usage.aggregate(rows, "day")
            today_key = date.today().isoformat()
            today_bucket = by_day.get(today_key, codex_usage.empty_bucket())
            window = [
                ((date.today() - timedelta(days=offset)).isoformat(),
                 by_day.get((date.today() - timedelta(days=offset)).isoformat(),
                            codex_usage.empty_bucket())["total_tokens"])
                for offset in range(6, -1, -1)
            ]
            titles = codex_usage.read_thread_titles(self.codex_home)
            today_rows = [row for row in rows if row["date"] == today_key]
            by_thread_today = codex_usage.aggregate(today_rows, "thread")
            by_log_today = codex_usage.aggregate(today_rows, "file")
            # the model in use right now: the most recent record in the logs
            active_model = max(rows, key=lambda row: row.get("timestamp") or "").get("model", "") if rows else ""

            leader = ""
            if by_thread_today:
                best_key = max(by_thread_today.items(),
                               key=lambda item: item[1]["total_tokens"])[0]
                bare = best_key.replace("[agent] ", "")
                leader = titles.get(bare) or bare[:8]

            self.results.put({
                "kind": "scan",
                "today": {key: today_bucket.get(key, 0) for key in USAGE_KEYS},
                "today_cost": today_bucket.get("cost", 0.0),
                "window": window,
                "threads": {key: bucket["total_tokens"]
                            for key, bucket in by_thread_today.items()},
                "thread_costs": {key: bucket.get("cost", 0.0)
                                 for key, bucket in by_thread_today.items()},
                "logs": {key: bucket["total_tokens"]
                         for key, bucket in by_log_today.items()},
                "leader": leader,
                "model": active_model,
                "files": stats["files"],
                "tail_path": newest_rollout(self.codex_home),
            })
        except Exception as error:  # keep the widget alive on a bad scan
            self.results.put({"kind": "error", "message": f"{type(error).__name__}: {error}"})

    def _live_tick(self) -> None:
        try:
            if stop_requested(self.stop_event):
                self.root.destroy()
                return
            self.ticks += 1
            delta = self.tail.poll()
            if delta and delta.get("reset"):
                self.refresh_scan()
            elif delta:
                self._apply_live(delta)
        except Exception:
            pass
        self.root.after(max(int(self.options.live * 1000), 200), self._live_tick)

    def _apply_live(self, delta: dict) -> None:
        for key in USAGE_KEYS:
            self.today[key] = self.today.get(key, 0) + delta.get(key, 0)
        live_cost = float(delta.get("cost") or 0.0)
        if self.pricing:
            self.today_cost += live_cost
        thread = str(delta.get("thread") or "")
        if thread:
            self.active_thread = thread
            self.live_threads[thread] = self.live_threads.get(thread, 0) \
                + delta.get("total_tokens", 0)
            self.live_costs[thread] = self.live_costs.get(thread, 0.0) + live_cost
        self.session_total = self._thread_total(self.active_thread)
        self.session_cost = self._thread_cost(self.active_thread)
        if self.window7:
            day, value = self.window7[-1]
            self.window7[-1] = (day, value + delta.get("total_tokens", 0))
        self._render_numbers()
        self._draw_spark()

    def _thread_total(self, thread: str) -> int:
        if not thread:
            return 0
        for key in (thread, f"[agent] {thread}"):
            if key in self.log_totals or key in self.live_threads:
                return self.log_totals.get(key, 0) + self.live_threads.get(key, 0)
        return 0

    def _thread_cost(self, thread: str) -> float:
        """Cost of one conversation, from the scan plus anything tailed since."""
        if not thread:
            return 0.0
        for key in (thread, f"[agent] {thread}"):
            if key in self.thread_costs or key in self.live_costs:
                return self.thread_costs.get(key, 0.0) + self.live_costs.get(key, 0.0)
        return 0.0

    # --------------------------------------------------------------- rendering
    def _drain(self) -> None:
        try:
            while True:
                payload = self.results.get_nowait()
                if payload.get("kind") == "scan":
                    self._render_scan(payload)
                else:
                    self.scanning = False
                    self.total.configure(text="读取失败", fg="#ff7b72")
                    self.split.configure(text=payload.get("message", "")[:44])
                    self.footer.configure(text="右键菜单可重试")
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def _render_scan(self, payload: dict) -> None:
        self.scanning = False
        self.today = dict(payload["today"])
        self.today_cost = float(payload.get("today_cost") or 0.0)
        self.window7 = list(payload["window"])
        self.thread_totals = dict(payload["threads"])
        self.thread_costs = dict(payload.get("thread_costs") or {})
        self.log_totals = dict(payload["logs"])
        self.live_threads = {}  # the scan has absorbed everything counted so far
        self.live_costs = {}
        self.files = payload["files"]
        self.leader = payload["leader"][:16]
        self.model = payload.get("model") or self.model
        self.tail.default_model = self.model
        self.tail.rebase(payload["tail_path"])
        self.active_thread = self.tail.active_thread or self.active_thread
        self.session_total = self._thread_total(self.active_thread)
        self.session_cost = self._thread_cost(self.active_thread)
        self._render_numbers()
        self._draw_spark()
        self.dock_state.configure(text="已贴客户端" if self.client else "未找到客户端")

    def _render_numbers(self) -> None:
        bucket = self.today
        uncached = bucket["input_tokens"] - bucket["cached_input_tokens"]
        hit = (bucket["cached_input_tokens"] / bucket["input_tokens"]
               if bucket["input_tokens"] else 0.0)
        self.total.configure(text=human(bucket["total_tokens"]), fg=FG)
        self.total_cost.configure(
            text=money_text(self.today_cost, self.currency) if self.pricing else "")
        self.hit.configure(text=f"缓存命中 {hit * 100:.1f}%")
        self.hit_detail.configure(text=f"省 {human(bucket['cached_input_tokens'])}")
        self.split.configure(
            text=f"新增 {human(uncached)} · 缓存 {human(bucket['cached_input_tokens'])}"
                 f" · 输出 {human(bucket['output_tokens'])}"
        )
        session_text = f"本次会话 {human(self.session_total)}"
        if self.pricing:
            session_text += f" · {money_text(self.session_cost, self.currency)}"
        self.session.configure(text=session_text)
        self.top_thread.configure(text=f"最费会话 {self.leader}" if self.leader else "")
        self._draw_hit_bar(hit)
        footer = (f"{datetime.now().strftime('%H:%M:%S')} · 实时 {self.options.live:g}s"
                  f" · {self.files} 日志")
        if self.pricing:
            footer = (f"{deepseek_pricing.resolve(self.model) or '价目未知'}"
                      f" · {self.currency} · {footer}")
        self.footer.configure(text=footer)

    def _draw_hit_bar(self, ratio: float) -> None:
        self.hit_bar.delete("all")
        width = max(self.hit_bar.winfo_width(), 206)
        self.hit_bar.create_rectangle(0, 0, int(width * min(max(ratio, 0.0), 1.0)), 5,
                                      fill=ACCENT, outline="")

    def _draw_spark(self) -> None:
        self.spark.delete("all")
        self.spark.update_idletasks()
        width = max(self.spark.winfo_width(), 180)
        height = int(self.spark["height"])
        self.spark.create_text(4, 2, anchor="nw", text="近 7 天", fill=FG_DIM,
                               font=("Segoe UI", 7))
        peak = max((value for _day, value in self.window7), default=0) or 1
        available = height - RAW - 2
        slot = width / max(len(self.window7), 1)
        bar_width = max(int(slot * 0.62), 4)
        for index, (day, value) in enumerate(self.window7):
            scaled = max(int((value / peak) * available), 3)
            x = int(index * slot + (slot - bar_width) / 2)
            if index == len(self.window7) - 1:
                colour = BAR_TODAY
            elif value == 0:
                colour = BAR_EMPTY
            else:
                colour = BAR
            self.spark.create_rectangle(x, height - scaled, x + bar_width, height - 1,
                                        fill=colour, outline="")

    def selftest(self) -> int:
        for _ in range(80):
            self.root.update()
            if not self.scanning and self.today["input_tokens"]:
                break
            time.sleep(0.1)
        self._follow_once()
        child = window_rect(self.hwnd)
        parent = self.client_rect
        inside = bool(child and parent and child[0] >= parent[0] and child[1] >= parent[1]
                      and child[2] <= parent[2] and child[3] <= parent[3])
        hit = (self.today["cached_input_tokens"] / self.today["input_tokens"]
               if self.today["input_tokens"] else 0)

        # a drag may never leave the client window, however far the cursor goes
        def drag_to(x_root: int, y_root: int) -> None:
            grab = type("Event", (), {"x_root": x_root, "y_root": y_root})()
            self._drag_start(grab)
            grab.x_root, grab.y_root = x_root, y_root
            self._drag_move(grab)
            self._drag_end(grab)
            self.root.update()

        escapes = []
        for x_root, y_root in ((10 ** 5, 10 ** 5), (-10 ** 5, -10 ** 5),
                               (parent[2] + 400, parent[3] + 400),
                               (parent[0] - 400, parent[3] // 2)):
            drag_to(x_root, y_root)
            box = window_rect(self.hwnd)
            escapes.append(bool(box and parent and box[0] >= parent[0] and box[1] >= parent[1]
                                 and box[2] <= parent[2] and box[3] <= parent[3]))
        self._redock()
        self.root.update()  # let Tk apply the geometry before reading it back
        redocked = window_rect(self.hwnd)

        print(f"client hwnd={self.client} rect={parent}")
        print(f"widget rect={child} inside_client={inside}")
        print(f"gap right={parent[2] - child[2] if child and parent else None}, "
              f"bottom={parent[3] - child[3] if child and parent else None}")
        print(f"drag clamp inside_client={escapes} all_inside={all(escapes)}")
        print(f"redock rect={redocked} margin={self.options.margin}")
        print(f"today total={self.today['total_tokens']} hit={hit * 100:.2f}% "
              f"session={self.session_total} files={self.files}")
        print(f"today cost={self.today_cost:.6f} {self.currency} "
              f"session_cost={self.session_cost:.6f} {self.currency} "
              f"model={self.model or 'unknown'} priced={self.pricing} "
              f"locked={self.locked}")
        self.root.destroy()
        return 0

    def _follow_once(self) -> None:
        self._attach_client()
        if self.client:
            set_owner(self.hwnd, self.client)
            self.root.deiconify()
            self._apply_position()
        self.root.update()

    def watch(self, seconds: float) -> int:
        """Headless harness: print the headline numbers whenever they change."""
        self.root.withdraw()
        deadline = time.monotonic() + seconds
        seen = None
        while time.monotonic() < deadline:
            self.root.update()
            snapshot = (self.today["total_tokens"], self.today["cached_input_tokens"],
                        self.today["input_tokens"], self.today["output_tokens"],
                        self.session_total, self.active_thread, self.leader,
                        round(self.today_cost, 6), round(self.session_cost, 6))
            if snapshot != seen:
                seen = snapshot
                hit = snapshot[1] / snapshot[2] * 100 if snapshot[2] else 0.0
                uncached = snapshot[2] - snapshot[1]
                print(f"{datetime.now().strftime('%H:%M:%S')} total={human(snapshot[0])} "
                      f"hit={hit:.2f}% new={human(uncached)} out={human(snapshot[3])} "
                      f"session={human(snapshot[4])} thread={snapshot[5][:8]} "
                      f"today={snapshot[7]:.4f} session={snapshot[8]:.4f} "
                      f"{self.currency} "
                      f"leader={snapshot[6][:12]} polls={self.ticks}")
            time.sleep(0.2)
        self.root.destroy()
        return 0

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", type=float, default=1.0, help="live tail interval, seconds")
    parser.add_argument("--rescan", type=int, default=60, help="seconds between full rescans")
    parser.add_argument("--follow-ms", type=int, default=250, help="client tracking interval, ms")
    parser.add_argument("--margin", type=int, default=14, help="gap inside the client corner, px")
    parser.add_argument("--opacity", type=float, default=0.95)
    parser.add_argument("--client-exe", action="append", default=None,
                        help="client process to dock to (repeatable, default ChatGPT.exe/Codex.exe)")
    parser.add_argument("--stay-visible", action="store_true",
                        help="keep the panel visible even when the client is not focused")
    parser.add_argument("--no-topmost", action="store_true")
    parser.add_argument("--codex-home", default=str(codex_usage.default_codex_home()))
    parser.add_argument("--no-price", action="store_true",
                        help="hide the cost readout and show tokens only")
    parser.add_argument("--usd", action="store_true",
                        help="price in DeepSeek's USD list prices instead of CNY")
    parser.add_argument("--allow-close", action="store_true",
                        help="development switch: leave the panel closable (default: locked)")
    parser.add_argument("--selftest", action="store_true", help="print geometry and exit")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                        help="headless: print the numbers whenever they change, then exit")
    options = parser.parse_args(argv)
    options.client_exe = tuple(options.client_exe or ("ChatGPT.exe", "Codex.exe"))
    force_utf8_output()
    if not options.selftest and not options.watch and not claim_single_instance():
        print("usage widget is already running", file=sys.stderr)
        return 0
    widget = UsageWidget(options)
    if options.selftest:
        return widget.selftest()
    if options.watch:
        return widget.watch(options.watch)
    return widget.run() or 0


if __name__ == "__main__":
    raise SystemExit(main())
