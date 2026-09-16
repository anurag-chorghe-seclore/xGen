"""
Inspect Mode Controller.
Coordinates hover polling, crosshair cursor, highlight overlay, and element selection.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional
from PyQt6.QtCore import QObject, QPoint, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication

from xgen.capture.mouse_hook import MouseHook
from xgen.capture.overlay_window import OverlayWindow
from xgen.config import XGenConfig
from xgen.core.tree_cache import TreeCacheStore
from xgen.core.tree_parser import TreeParser
from xgen.core.uia_bridge import UIABridge, UIAElement
from xgen.utils.dpi import get_physical_cursor_pos

logger = logging.getLogger("xgen.inspect")


class InspectMode(QObject):
    """
    Inspect mode state machine and hover tracker.
    """
    mode_changed = pyqtSignal(bool)           # True=active, False=idle
    hovering = pyqtSignal(object)             # UIAElement
    element_clicked = pyqtSignal(object, int, int)  # UIAElement, click_x, click_y

    def __init__(
        self,
        config: XGenConfig,
        overlay: Optional[OverlayWindow] = None,
        mouse_hook: Optional[MouseHook] = None,
        parent: Optional[QObject] = None
    ):
        super().__init__(parent)
        self.config = config
        self.overlay = overlay or OverlayWindow()
        self.mouse_hook = mouse_hook or MouseHook(self)

        self._is_active = False
        self._last_uia_el: Optional[UIAElement] = None
        self._window_filter: Optional[Callable[[int, int], bool]] = None

        # Hover polling timer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.config.hover_poll_interval_ms)
        self._poll_timer.timeout.connect(self._poll_hover)

        # Mouse click signal
        self.mouse_hook.left_clicked.connect(self._on_mouse_click)

    @property
    def is_active(self) -> bool:
        return self._is_active

    def set_window_filter(self, filter_fn: Callable[[int, int], bool]) -> None:
        """Filter to exclude clicks/hovers over xGen's own UI windows."""
        self._window_filter = filter_fn

    def toggle(self) -> bool:
        """Toggle inspect mode between active and idle."""
        if self._is_active:
            self.deactivate()
        else:
            self.activate()
        return self._is_active

    def activate(self) -> None:
        """Enter Inspect Mode: set crosshair cursor, show overlay, start polling and mouse hook."""
        if self._is_active:
            return

        logger.info("Entering Inspect Mode (F3).")
        self._is_active = True
        self._last_uia_el = None

        # Set crosshair cursor application-wide
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.CrossCursor))

        # Start mouse hook with window filter
        self.mouse_hook.start(self._window_filter)

        # Start hover polling timer
        self._poll_timer.start(self.config.hover_poll_interval_ms)
        self.mode_changed.emit(True)

    def deactivate(self, keep_overlay: bool = False) -> None:
        """Exit Inspect Mode: restore cursor, stop polling and hook."""
        if not self._is_active:
            return

        logger.info("Exiting Inspect Mode.")
        self._is_active = False
        self._poll_timer.stop()
        self.mouse_hook.stop()
        if not keep_overlay:
            self.overlay.hide_overlay()

        # Restore default cursor
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

        self.mode_changed.emit(False)

    def _poll_hover(self) -> None:
        """Periodic hover check under mouse cursor."""
        if not self._is_active:
            return

        x, y = get_physical_cursor_pos()

        # Check if cursor is over xGen's own window
        if self._window_filter and not self._window_filter(x, y):
            # Preserve test-verified green box and selected blue box while cursor is inside xGen window
            if self.overlay.style_mode in ("tested", "selected"):
                return
            self.overlay.hide_overlay()
            return

        # 1. Check active cached XML tree for exact innermost leaf control
        cache = TreeCacheStore.instance().get_active()
        deepest_node = None
        if cache and cache.parsed_root:
            deepest_node = TreeParser.find_deepest_at_point(cache.parsed_root, x, y)

        # 2. Query live UIA element with child drill-down
        el = UIABridge.element_from_point(x, y)

        # If cached tree has a specific leaf control at (x, y), prioritize cached XML tree
        if deepest_node and deepest_node.bounding_rect and deepest_node.bounding_rect.area > 0:
            if el is None or (el.bounding_rect and deepest_node.bounding_rect.area <= el.bounding_rect.area):
                leaf_el = UIAElement(
                    runtime_id=deepest_node.runtime_id,
                    control_type=deepest_node.tag,
                    name=deepest_node.name,
                    automation_id=deepest_node.automation_id,
                    bounding_rect=deepest_node.bounding_rect
                )
                self._last_uia_el = leaf_el
                self.overlay.highlight_hover(deepest_node.bounding_rect)
                self.hovering.emit(leaf_el)
                return

        if el is not None:
            self._last_uia_el = el
            self.overlay.highlight_hover(el.bounding_rect)
            self.hovering.emit(el)
        else:
            self.overlay.hide_overlay()

    def _on_mouse_click(self, x: int, y: int) -> None:
        """Handle left-click event captured by global hook."""
        if not self._is_active:
            return

        target_el = self._last_uia_el
        if target_el is None:
            target_el = UIABridge.element_from_point(x, y)

        logger.info("Element clicked at (%d, %d): %s [%s]", x, y, target_el.control_type if target_el else "None", target_el.automation_id if target_el else "")

        # Keep Inspect Mode continuously active until explicitly toggled off (F3 / Esc / Toolbar)
        if target_el:
            self.element_clicked.emit(target_el, x, y)
