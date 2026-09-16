"""
Transient Element Capture Engine.
Provides F4 Instant Snapshot, Timed Countdown Capture, Structure Changed Hook, and Frozen Tree Mode.
"""

from __future__ import annotations

import datetime
import logging
from collections import deque
from typing import List, Optional, Set
from PyQt6.QtCore import QObject, QThreadPool, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCursor

try:
    import win32api
    HAS_PYWIN32 = True
except ImportError:
    win32api = None        # type: ignore
    HAS_PYWIN32 = False

import uiautomation as auto

from xgen.core.tree_cache import TreeCacheStore
from xgen.core.tree_parser import TreeParser, UINode
from xgen.core.uia_bridge import UIABridge, UIAElement
from xgen.utils.dpi import get_physical_cursor_pos
from xgen.utils.rect import Rect

logger = logging.getLogger("xgen.transient")


class TransientCapturer(QObject):
    """
    Orchestrates transient and ephemeral UI capture across all 4 mechanisms.
    """
    transient_captured = pyqtSignal(object)       # UINode (merged transient root)
    transient_failed = pyqtSignal(str)            # Failure reason
    timed_capture_tick = pyqtSignal(int)          # seconds remaining
    freeze_state_changed = pyqtSignal(bool)       # True=frozen, False=live

    TRANSIENT_TYPES: Set[str] = {
        "Menu",
        "MenuItem",
        "ToolTip",
        "Window",
        "List",
        "Pane",
        "Popup",
        "Flyout",
    }

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._is_frozen = False
        self._countdown_timer: Optional[QTimer] = None
        self._seconds_left = 0

    @property
    def is_frozen(self) -> bool:
        return self._is_frozen

    def set_frozen(self, frozen: bool) -> None:
        """Toggle frozen tree mode."""
        if self._is_frozen != frozen:
            self._is_frozen = frozen
            logger.info("Frozen tree mode: %s", "ENABLED" if frozen else "DISABLED")
            self.freeze_state_changed.emit(frozen)

    # --- Mechanism 1: F4 Instant Freeze Snapshot ---

    def freeze_snapshot(self, cursor_x: int, cursor_y: int, active_handle: str = "") -> None:
        """
        Instant in-process snapshot of the hovered transient element.
        Executed without stealing window focus.
        """
        logger.info("F4 Freeze Snapshot triggered at (%d, %d)", cursor_x, cursor_y)
        try:
            ctrl = auto.ControlFromPoint(cursor_x, cursor_y)
            if ctrl is None:
                logger.warning("F4: No control found under cursor.")
                self.transient_failed.emit("No control found under cursor.")
                return

            if getattr(ctrl, "Name", "") == "Desktop" or getattr(ctrl, "ClassName", "") == "#32769":
                logger.info("F4: Cursor is over desktop background.")
                self.transient_failed.emit("Cursor is over desktop background. Hover over an application element.")
                return

            SPECIFIC_TRANSIENT = {"Menu", "ToolTip", "Popup", "Flyout", "MenuItem"}

            # Walk up to the topmost transient container (popup window or menu)
            top_transient = ctrl
            curr = ctrl
            while curr:
                ct = curr.ControlTypeName.replace("Control", "")
                if ct in SPECIFIC_TRANSIENT:
                    top_transient = curr
                parent = curr.GetParentControl()
                if not parent or getattr(parent, "Name", "") == "Desktop":
                    class_name = getattr(curr, "ClassName", "")
                    if class_name in ("#32768", "tooltips_class32") or "Popup" in class_name or "DropDown" in class_name:
                        top_transient = curr
                    break
                parent_ct = parent.ControlTypeName.replace("Control", "")
                if parent_ct == "Window" and ct not in SPECIFIC_TRANSIENT:
                    break
                curr = parent

            # Walk entire transient subtree
            elements = UIABridge.walk_subtree(root_ctrl=top_transient)
            if not elements:
                # Fallback to single element snapshot
                single_el = UIABridge._control_to_element(top_transient)
                if single_el:
                    elements = [single_el]

            if elements:
                # Convert to hierarchical UINode tree
                transient_root = self._uia_list_to_tree(elements)
                # Merge into active window cache
                TreeCacheStore.instance().merge_transient(active_handle, transient_root)
                self.transient_captured.emit(transient_root)
                logger.info("F4: Successfully captured %d transient elements.", len(elements))
            else:
                self.transient_failed.emit("No UI elements found under cursor.")
        except Exception as e:
            logger.exception("F4 Freeze Snapshot error: %s", e)
            self.transient_failed.emit(f"Capture failed: {e}")

    # --- Mechanism 2: Timed Countdown Capture ---

    def start_timed_capture(self, delay_seconds: int = 5, active_handle: str = "") -> None:
        """Start countdown for timed capture."""
        self.cancel_timed_capture()
        self._seconds_left = delay_seconds
        self.timed_capture_tick.emit(self._seconds_left)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(lambda: self._on_timed_tick(active_handle))
        self._countdown_timer.start()

    def cancel_timed_capture(self) -> None:
        """Abort active countdown."""
        if self._countdown_timer:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self._seconds_left = 0

    def _on_timed_tick(self, active_handle: str) -> None:
        self._seconds_left -= 1
        self.timed_capture_tick.emit(self._seconds_left)

        if self._seconds_left <= 0:
            self.cancel_timed_capture()
            # Perform desktop-wide transient scan
            x, y = get_physical_cursor_pos()
            self.freeze_snapshot(x, y, active_handle)

    # --- Mechanism 3: Automatic Structure Changed Hook ---

    def on_structure_changed(self, uia_el: UIAElement, change_type: str) -> None:
        """Triggered automatically when UI Automation fires ChildAdded event."""
        if change_type != "ChildAdded" or uia_el.control_type not in self.TRANSIENT_TYPES:
            return

        logger.info("Auto-Structure Hook: Detected new %s", uia_el.control_type)
        elements = UIABridge.walk_subtree(root_element=uia_el)
        if elements:
            transient_root = self._uia_list_to_tree(elements)
            TreeCacheStore.instance().merge_transient("", transient_root)
            self.transient_captured.emit(transient_root)

    # --- Helper: Convert Flat UIAElement List to UINode Tree ---

    def _uia_list_to_tree(self, elements: List[UIAElement]) -> UINode:
        """Convert flat BFS UIAElement list to a UINode tree marked as transient."""
        if not elements:
            return UINode(tag="Menu", attributes={"Name": "Transient"}, is_transient=True)

        now = datetime.datetime.now()
        root_el = elements[0]

        root_node = UINode(
            tag=root_el.control_type,
            attributes={
                "ControlType": root_el.control_type,
                "AutomationId": root_el.automation_id,
                "Name": root_el.name,
                "ClassName": root_el.class_name,
                "RuntimeId": root_el.runtime_id,
                "IsEnabled": str(root_el.is_enabled),
            },
            bounding_rect=root_el.bounding_rect,
            runtime_id=root_el.runtime_id,
            is_transient=True,
            captured_at=now
        )

        # Attach child elements
        for child_el in elements[1:]:
            child_node = UINode(
                tag=child_el.control_type,
                attributes={
                    "ControlType": child_el.control_type,
                    "AutomationId": child_el.automation_id,
                    "Name": child_el.name,
                    "ClassName": child_el.class_name,
                    "RuntimeId": child_el.runtime_id,
                    "IsEnabled": str(child_el.is_enabled),
                },
                parent=root_node,
                bounding_rect=child_el.bounding_rect,
                runtime_id=child_el.runtime_id,
                is_transient=True,
                captured_at=now
            )
            root_node.children.append(child_node)

        return root_node
