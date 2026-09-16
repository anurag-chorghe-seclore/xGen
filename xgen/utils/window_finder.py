"""
Window Finder Utility.
Enumerates visible top-level application windows using pure ctypes (no pywin32 required).
Returns only Desktop Root on non-Windows platforms.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("xgen.window_finder")


def normalize_handle(h: Any) -> Optional[int]:
    """
    Normalize any window handle representation (int, decimal str, hex str, HWND)
    to a raw integer HWND, or None if empty / Root / invalid.
    """
    if h is None:
        return None
    s = str(h).strip()
    if not s or s.lower() in ("root", "none", "0"):
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
    except (ValueError, TypeError):
        return None


@dataclass
class WindowTarget:
    """Represents a single inspectable top-level window or the Desktop Root pseudo-target."""
    title: str
    handle_hex: str   # e.g. "0x000E07FA"  (empty for Desktop Root)
    hwnd: int         # Raw HWND integer (0 for Desktop Root)
    exe_name: str     # e.g. "notepad.exe"  (empty for Desktop Root)
    is_root: bool = field(default=False)

    def display_label(self) -> str:
        if self.is_root:
            return "Desktop Root (Entire OS)"
        exe = f" [{self.exe_name}]" if self.exe_name else ""
        return f"{self.title}{exe}"


_DESKTOP_ROOT = WindowTarget(
    title="Desktop Root (Entire OS)",
    handle_hex="",
    hwnd=0,
    exe_name="",
    is_root=True,
)


def get_open_windows() -> List[WindowTarget]:
    """
    Return a list of visible top-level application windows.

    Index 0 is always the Desktop Root pseudo-target.
    Remaining entries are real application windows, sorted alphabetically by title.
    On non-Windows platforms returns only [Desktop Root].
    """
    results: List[WindowTarget] = [_DESKTOP_ROOT]

    if sys.platform != "win32":
        return results

    try:
        results.extend(_enumerate_windows_win32())
    except Exception:
        logger.exception("Window enumeration failed; returning Desktop Root only.")

    return results


# ---------------------------------------------------------------------------
# Windows-only ctypes implementation
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _user32 = ctypes.windll.user32      # type: ignore[attr-defined]
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    _EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )


def _get_exe_name(hwnd: int) -> str:
    """Resolve the basename of the executable owning this window."""
    pid = ctypes.wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h_proc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h_proc:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    size = ctypes.wintypes.DWORD(512)
    try:
        if _kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    except Exception:
        pass
    finally:
        _kernel32.CloseHandle(h_proc)
    return ""


def _enumerate_windows_win32() -> List[WindowTarget]:
    """Walk all top-level windows via EnumWindows, keeping only visible app windows."""
    own_pid = os.getpid()
    found: List[WindowTarget] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True

        # Skip windows with empty titles
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True

        # Skip zero-area windows (hidden or minimised-to-nothing)
        rect = ctypes.wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if (rect.right - rect.left) <= 0 or (rect.bottom - rect.top) <= 0:
            return True

        # Skip our own process
        pid = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True

        found.append(WindowTarget(
            title=title,
            handle_hex=hex(hwnd),
            hwnd=hwnd,
            exe_name=_get_exe_name(hwnd),
        ))
        return True

    proc = _EnumWindowsProc(_callback)
    _user32.EnumWindows(proc, 0)
    found.sort(key=lambda w: w.title.lower())
    return found
