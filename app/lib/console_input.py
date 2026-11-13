#!/usr/bin/env python3
"""Cross-platform console input for the plugin TUI dashboards.

The TUI instance used termios + tty.setcbreak + select directly (Linux/macOS).
The Herdr App also runs on Windows, where those modules do not exist. This
shim keeps the exact same contract the dashboards already use:

    with ConsoleInput() as console:
        if console.wait(0.25):
            key, mouse = console.read()   # ("r", None) / ("mouse", (x, y))

POSIX behaviour is byte-for-byte the TUI implementation (cbreak, 80 ms escape
aggregation, SGR mouse ``\\x1b[<0;x;yM``). On Windows the same keys are read
through msvcrt with virtual-terminal input enabled; SGR mouse reports are
parsed when the console delivers them, otherwise the dashboards stay fully
keyboard-operable (r/s/d/q), which is their documented fallback.
"""

from __future__ import annotations

import os
import re
import sys
import time

IS_WINDOWS = os.name == "nt"

if not IS_WINDOWS:
    import select
    import termios
    import tty


class ConsoleInput:
    def __init__(self) -> None:
        self._saved = None
        self._fd = sys.stdin.fileno() if sys.stdin.isatty() else None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ConsoleInput":
        if self._fd is None:
            return self
        if IS_WINDOWS:
            self._enable_vt_input()
        else:
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is None or self._saved is None:
            return
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        self._saved = None

    @staticmethod
    def _enable_vt_input() -> None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200); keep the rest.
                kernel32.SetConsoleMode(handle, mode.value | 0x0200)
        except Exception:
            pass

    # -- reading -----------------------------------------------------------

    def wait(self, timeout: float) -> bool:
        """True when a key (or escape sequence) is ready within timeout."""
        if self._fd is None:
            time.sleep(timeout)
            return False
        if IS_WINDOWS:
            import msvcrt

            deadline = time.monotonic() + timeout
            while True:
                if msvcrt.kbhit():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.02, remaining))
        ready, _, _ = select.select([self._fd], [], [], timeout)
        return bool(ready)

    def read(self) -> tuple[str, tuple[int, int] | None]:
        """Return (key, mouse_position). key: char, "escape" or "mouse"."""
        if self._fd is None:
            return "", None
        if IS_WINDOWS:
            return self._read_windows()
        return self._read_posix()

    def _read_posix(self) -> tuple[str, tuple[int, int] | None]:
        first = os.read(self._fd, 1)
        if first != b"\x1b":
            return first.decode("utf-8", "ignore"), None
        data = bytearray(first)
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline and len(data) < 48:
            ready, _, _ = select.select([self._fd], [], [], 0.01)
            if not ready:
                continue
            data.extend(os.read(self._fd, 16))
            if data.endswith((b"M", b"m")):
                break
        match = re.fullmatch(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", bytes(data))
        if match and match.group(4) == b"M" and int(match.group(1)) == 0:
            return "mouse", (int(match.group(2)), int(match.group(3)))
        return "escape", None

    def _read_windows(self) -> tuple[str, tuple[int, int] | None]:
        import msvcrt

        first = msvcrt.getch()
        if first in (b"\x00", b"\xe0"):
            # Special-key prefix: arrows/function keys. Map to the escape
            # sequences the POSIX path produces so callers stay unchanged.
            second = msvcrt.getch()
            mapping = {b"H": "\x1b[A", b"P": "\x1b[B", b"M": "\x1b[C", b"K": "\x1b[D"}
            return mapping.get(second, "escape"), None
        if first != b"\x1b":
            return first.decode("utf-8", "ignore"), None
        # Escape sequence: aggregate briefly, exactly like the POSIX path.
        data = bytearray(first)
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline and len(data) < 48:
            if not msvcrt.kbhit():
                time.sleep(0.005)
                continue
            data.extend(msvcrt.getch())
            if data.endswith((b"M", b"m")):
                break
        match = re.fullmatch(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", bytes(data))
        if match and match.group(4) == b"M" and int(match.group(1)) == 0:
            return "mouse", (int(match.group(2)), int(match.group(3)))
        return "escape", None


def terminal_size(default: tuple[int, int] = (80, 24)) -> tuple[int, int]:
    import shutil

    size = shutil.get_terminal_size(default)
    return size.columns, size.lines
