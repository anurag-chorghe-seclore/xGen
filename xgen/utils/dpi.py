"""
DPI Awareness and scaling helpers.

init_dpi_awareness, get_physical_cursor_pos, and get_dpi_for_window delegate
their native part to the current PlatformBackend (see
xgen/platform/windows_backend.py). get_screen_dpr_at and physical_to_logical_rect
are pure Qt math with no OS dependency and stay here unchanged.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

from xgen.utils.rect import Rect

logger = logging.getLogger("xgen.dpi")


def init_dpi_awareness() -> None:
    """Initialize per-monitor DPI awareness before any UI or window creation. No-op where not applicable."""
    from xgen.platform.factory import get_platform_backend
    get_platform_backend().init_dpi_awareness()


def get_physical_cursor_pos() -> tuple[int, int]:
    """Return the current mouse cursor position in physical screen coordinates."""
    from xgen.platform.factory import get_platform_backend
    return get_platform_backend().get_physical_cursor_pos()


def get_screen_dpr_at(x: int, y: int) -> float:
    """
    Return the factor for converting this platform's native screen coordinates
    into Qt's logical space — 1.0 when no conversion is needed.

    Every physical->logical conversion in xGen funnels through here, so this is
    the single place that decides whether scaling happens at all. On Windows
    native coordinates are physical pixels and the display's device pixel ratio
    applies. On macOS the accessibility API and Appium's Mac2 driver both report
    points, which is already Qt's space, so converting would put the three
    coordinate sources out of step with each other by exactly the scale factor.
    """
    from xgen.platform.factory import get_platform_backend
    try:
        if not get_platform_backend().uses_physical_pixel_coords():
            return 1.0
    except Exception:
        pass  # fall through to the measured ratio

    app = QApplication.instance()
    if not app:
        return 1.0

    # 1. Match physical screen bounds across all active displays
    screens = app.screens()
    for s in screens:
        dpr = float(s.devicePixelRatio()) or 1.0
        geom = s.geometry()  # logical geometry
        phys_left = int(geom.x() * dpr)
        phys_top = int(geom.y() * dpr)
        phys_right = phys_left + int(geom.width() * dpr)
        phys_bottom = phys_top + int(geom.height() * dpr)
        if phys_left <= x < phys_right and phys_top <= y < phys_bottom:
            return dpr
        if geom.left() <= x < geom.right() and geom.top() <= y < geom.bottom():
            return dpr

    screen = app.screenAt(QPoint(x, y))
    if screen is not None:
        return float(screen.devicePixelRatio())

    primary = app.primaryScreen()
    return float(primary.devicePixelRatio()) if primary else 1.0


def physical_to_logical_rect(rect: Rect) -> Rect:
    """Convert physical screen pixels (from native accessibility APIs) to Qt logical coordinates accounting for per-monitor DPI."""
    dpr = get_screen_dpr_at(rect.left, rect.top)
    if dpr == 1.0 or dpr <= 0.0:
        return rect

    lx = int(rect.left / dpr)
    ly = int(rect.top / dpr)
    lw = int(rect.width / dpr)
    lh = int(rect.height / dpr)
    return Rect(lx, ly, lx + lw, ly + lh)


def get_dpi_for_window(hwnd: int) -> int:
    """Return the DPI for the specified window handle (HWND). Defaults to 96."""
    if not hwnd:
        return 96
    from xgen.platform.factory import get_platform_backend
    return get_platform_backend().get_dpi_for_window(hwnd)


def get_scale_factor_for_window(hwnd: int) -> float:
    """Return the DPI scale factor (e.g. 1.0, 1.25, 1.5, 2.0) for the window."""
    return get_dpi_for_window(hwnd) / 96.0
