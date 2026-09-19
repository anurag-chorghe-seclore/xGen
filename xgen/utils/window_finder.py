"""
Window Finder Utility.

WindowTarget and normalize_handle are plain data/parsing helpers with no OS
dependency and stay here. The actual native window enumeration lives behind
xgen.platform.factory.get_platform_backend() (see xgen/platform/windows_backend.py)
so this module never touches ctypes/win32 directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


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


def get_open_windows() -> List[WindowTarget]:
    """
    Return a list of visible top-level application windows.

    Index 0 is always the Desktop Root pseudo-target. Remaining entries are
    real application windows, sorted alphabetically by title. Delegates to
    the current PlatformBackend; returns [Desktop Root] only where that
    backend has nothing more to offer (today: any non-Windows platform).
    """
    from xgen.platform.factory import get_platform_backend
    return get_platform_backend().get_open_windows()
