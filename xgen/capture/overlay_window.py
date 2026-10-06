"""
Highlight Overlay Window.
Transparent, click-through, always-on-top frameless window drawing a bounding rectangle.
"""

from __future__ import annotations

import logging
from typing import Optional
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QPaintEvent, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from xgen.platform.factory import get_platform_backend
from xgen.utils.dpi import get_screen_dpr_at, physical_to_logical_rect
from xgen.utils.rect import Rect

logger = logging.getLogger("xgen.overlay")


class OverlayWindow(QWidget):
    """
    Click-through highlight overlay window tracking hovered and selected element bounds.
    """
    HOVER_FILL = QColor(255, 68, 68, 50)        # semi-transparent coral
    HOVER_BORDER = QColor(255, 40, 40, 255)    # solid coral border

    SELECTED_FILL = QColor(0, 150, 255, 50)     # semi-transparent electric blue
    SELECTED_BORDER = QColor(0, 150, 255, 255)  # solid electric blue

    TESTED_FILL = QColor(16, 185, 129, 70)      # semi-transparent neon emerald
    TESTED_BORDER = QColor(16, 185, 129, 255)   # solid neon emerald border
    BORDER_WIDTH = 3

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._style_mode = "hover"  # "hover" | "selected" | "tested"
        self._current_rect: Optional[Rect] = None

    @property
    def style_mode(self) -> str:
        return self._style_mode

    def highlight_hover(self, rect: Optional[Rect]) -> None:
        """Display hover bounding box in coral highlight."""
        self._style_mode = "hover"
        self.move_to(rect)

    def highlight_selected(self, rect: Optional[Rect]) -> None:
        """Display selected bounding box in electric blue highlight."""
        self._style_mode = "selected"
        self.move_to(rect)

    def highlight_tested(self, rect: Optional[Rect]) -> None:
        """Display test-verified bounding box in vibrant emerald green highlight."""
        self._style_mode = "tested"
        self.move_to(rect)

    def move_to(self, rect: Optional[Rect]) -> None:
        """Move and resize the overlay widget to match target element bounding box, accounting for DPI zoom."""
        if rect is None or rect.width <= 0 or rect.height <= 0:
            self.hide_overlay()
            return

        # Do not overlay full-screen desktop background to avoid flooding display with blue
        if self._is_fullscreen_rect(rect):
            self.hide_overlay()
            return

        self._current_rect = rect

        # Convert physical monitor pixels to Qt logical DPI coordinates
        log_rect = physical_to_logical_rect(rect)

        # Inset / expand slightly so border encloses element (allow negative coords on secondary monitors)
        gx = log_rect.left - 2
        gy = log_rect.top - 2
        gw = max(4, log_rect.width + 4)
        gh = max(4, log_rect.height + 4)

        self.setGeometry(gx, gy, gw, gh)

        if not self.isVisible():
            self.show()
            self._apply_native_styles()

        self._enforce_topmost()
        self.raise_()
        self.update()

    def _is_fullscreen_rect(self, rect: Rect) -> bool:
        """Check if bounding rect covers the entire monitor screen.

        `rect` is in the platform's *native* coordinate space, which is not the
        same thing everywhere: physical pixels on Windows, points on macOS.
        get_screen_dpr_at() is the single place that knows which, so the
        screen's logical geometry is scaled into the rect's own space through
        it. Hardcoding devicePixelRatio here instead made this test silently
        dead on every Retina Mac — the screen looked twice as large as any
        rect could be, so desktop-sized elements stopped being suppressed and
        flooded the display with a full-screen highlight.
        """
        app = QApplication.instance()
        if not app:
            return False
        cx = rect.left + rect.width // 2
        cy = rect.top + rect.height // 2
        dpr = get_screen_dpr_at(cx, cy) or 1.0
        # screenAt() wants logical coordinates; cx/cy are native.
        screen = app.screenAt(QPoint(int(cx / dpr), int(cy / dpr))) or app.primaryScreen()
        if screen:
            geom = screen.geometry()
            nat_w = int(geom.width() * dpr)
            nat_h = int(geom.height() * dpr)
            # If rect spans the whole monitor from corner to corner
            if abs(rect.left - int(geom.left() * dpr)) <= 10 and abs(rect.top - int(geom.top() * dpr)) <= 10:
                if rect.width >= (nat_w - 40) and rect.height >= (nat_h - 40):
                    return True
        return False

    def hide_overlay(self) -> None:
        """Hide overlay without destroying."""
        self._current_rect = None
        self.hide()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw filled highlight rectangle and crisp border based on active mode."""
        if not self._current_rect:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        if self._style_mode == "tested":
            fill_color = self.TESTED_FILL
            border_color = self.TESTED_BORDER
        elif self._style_mode == "hover":
            fill_color = self.HOVER_FILL
            border_color = self.HOVER_BORDER
        else:
            fill_color = self.SELECTED_FILL
            border_color = self.SELECTED_BORDER

        # Draw semi-transparent background fill
        painter.fillRect(self.rect(), fill_color)

        # Draw crisp solid outer border
        pen = QPen(border_color, self.BORDER_WIDTH)
        painter.setPen(pen)
        draw_rect = self.rect().adjusted(1, 1, -2, -2)
        painter.drawRect(draw_rect)

    def _apply_native_styles(self) -> None:
        """Apply native click-through styling safely without disrupting Qt alpha compositing. No-op where not applicable."""
        get_platform_backend().apply_click_through(self)

    def _enforce_topmost(self) -> None:
        """Raise window to the top of the Z-band above context menus without overriding Qt DPI geometry. No-op where not applicable."""
        get_platform_backend().enforce_topmost(self)

