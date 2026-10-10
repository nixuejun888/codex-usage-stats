#!/usr/bin/env python3
"""Make sure the docked usage panel is running, then return immediately.

Wired to SessionStart by hooks/hooks.json. Hooks must not block the session, so
this script never becomes the panel itself: it checks the panel's single-instance
mutex and, only when nothing is running, spawns the panel fully detached so it
outlives the hook process.

    pythonw hooks/ensure_widget.py            # start if needed (what the hook runs)
    pythonw hooks/ensure_widget.py --status   # print running/idle, start nothing
    pythonw hooks/ensure_widget.py --stop     # the panel is locked; this is how it goes away
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
from pathlib import Path

ERROR_FILE_NOT_FOUND = 2
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
WIDGET_MUTEX_NAME = r"Local\CodexUsageStatsWidget"
STOP_EVENT_NAME = r"Local\CodexUsageStatsStop"


def plugin_root() -> Path:
    env = os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def panel_running() -> bool:
    """True when a panel already holds the single-instance mutex."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = wt.HANDLE
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, WIDGET_MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    # ERROR_FILE_NOT_FOUND means "no such mutex"; anything else is unexpected,
    # and reporting it as running is the safe side to fail on (no spawn storm).
    return ctypes.get_last_error() != ERROR_FILE_NOT_FOUND


def window_less_python() -> str:
    """A python interpreter that opens no console window."""
    for candidate in ("pythonw.exe", "pythonw"):
        found = _which(candidate)
        if found:
            return found
    exe = Path(sys.executable)
    for name in ("pythonw.exe", "python.exe"):
        sibling = exe.with_name(name)
        if sibling.exists():
            return str(sibling)
    return sys.executable


def request_stop() -> bool:
    """Set the panel's stop event; True when a panel was there to notice it.

    The panel has no close button, so this is the deliberate way to shut it
    down. It is a signal rather than a kill: the panel owns its own window and
    exits cleanly on its next tick.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.restype = wt.HANDLE
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, STOP_EVENT_NAME)
    if not handle:
        return False
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


def _which(name: str) -> str:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder:
            continue
        candidate = Path(folder) / name
        if candidate.is_file():
            return str(candidate)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true",
                        help="print whether the panel is running; start nothing")
    parser.add_argument("--stop", action="store_true",
                        help="ask a running panel to exit (it has no close button)")
    parser.add_argument("--widget", default="", help="override the panel script path")
    options = parser.parse_args(argv)

    if options.stop:
        if not panel_running():
            print("idle")
            return 0
        print("stop requested" if request_stop() else "panel is running but would not take the signal")
        return 0

    running = panel_running()
    if options.status:
        print("running" if running else "idle")
        return 0
    if running:
        return 0

    root = plugin_root()
    widget = Path(options.widget) if options.widget else root / "scripts" / "usage_widget.py"
    if not widget.is_file():
        print(f"panel script not found: {widget}", file=sys.stderr)
        return 1

    subprocess.Popen(
        [window_less_python(), str(widget)],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
