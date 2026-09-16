"""
Windows UI Automation Native COM Bridge.
Provides in-process ElementFromPoint, TreeWalker, and Structure Changed Event monitoring.
"""

from __future__ import annotations

import ctypes
import logging
from collections import deque
from dataclasses import dataclass
from typing import List, Optional
from PyQt6.QtCore import QObject, QThread, pyqtSignal

try:
    import comtypes
    from ctypes import wintypes as _wintypes
    HAS_COMTYPES = True
except ImportError:
    comtypes = None        # type: ignore
    _wintypes = None       # type: ignore
    HAS_COMTYPES = False

import uiautomation as auto
from xgen.utils.rect import Rect

logger = logging.getLogger("xgen.uia")


@dataclass
class UIAElement:
    """Thread-safe snapshot of a UI Automation element."""
    runtime_id: str                      # "42.12345.0"
    control_type: str                    # "Button", "Edit", etc.
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    bounding_rect: Optional[Rect] = None
    is_enabled: bool = True
    native_handle: int = 0               # HWND or 0
    help_text: str = ""
    aria_properties: str = ""


class UIABridge(QObject):
    """
    Manages native UIA COM interop and structure event watching.
    """
    structure_changed = pyqtSignal(object, str)  # UIAElement, change_type

    @staticmethod
    def initialize() -> None:
        """Call on app startup to initialize COM runtime."""
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception as e:
            logger.debug("CoInitialize note: %s", e)

    INTERACTIVE_CONTROL_TYPES = {
        auto.ControlType.ButtonControl,
        auto.ControlType.MenuItemControl,
        auto.ControlType.TabItemControl,
        auto.ControlType.CheckBoxControl,
        auto.ControlType.RadioButtonControl,
        auto.ControlType.HyperlinkControl,
        auto.ControlType.ComboBoxControl,
        auto.ControlType.EditControl,
        auto.ControlType.ListItemControl,
        auto.ControlType.TreeItemControl,
        auto.ControlType.HeaderItemControl,
        auto.ControlType.SliderControl,
        auto.ControlType.ProgressBarControl,
        auto.ControlType.SpinnerControl,
    }

    @classmethod
    def _wake_chromium_accessibility(cls, hwnd: int) -> None:
        """
        Activates Chromium's Blink accessibility engine on demand.
        By default Chromium does not build accessibility nodes for web contents until queried.
        """
        if not hwnd or not HAS_COMTYPES or not comtypes or not _wintypes:
            return
        try:
            OBJID_CLIENT = 0xFFFFFFFC
            IID_IAccessible = comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")
            p_acc = ctypes.c_void_p()
            ctypes.windll.oleacc.AccessibleObjectFromWindow(
                _wintypes.HWND(hwnd),
                _wintypes.DWORD(OBJID_CLIENT & 0xFFFFFFFF),
                ctypes.byref(IID_IAccessible),
                ctypes.byref(p_acc)
            )
        except Exception:
            pass

    @classmethod
    def _drill_down(cls, start_ctrl: auto.Control, x: int, y: int) -> auto.Control:
        """Descends into innermost child element at (x, y), stopping on interactive controls."""
        best = start_ctrl
        for _ in range(12):
            if best.ControlType in cls.INTERACTIVE_CONTROL_TYPES:
                break
            children = best.GetChildren()
            found = False
            for child in children:
                if getattr(child, "ClassName", "") == "FrameGrabHandle":
                    continue
                r = child.BoundingRectangle
                if r.left <= x <= r.right and r.top <= y <= r.bottom:
                    cw = r.right - r.left
                    ch = r.bottom - r.top
                    bw = best.BoundingRectangle.right - best.BoundingRectangle.left
                    bh = best.BoundingRectangle.bottom - best.BoundingRectangle.top
                    if 0 < cw * ch < bw * bh:
                        best = child
                        found = True
                        break
            if not found:
                break
        return best

    @classmethod
    def element_from_point(cls, x: int, y: int) -> Optional[UIAElement]:
        """
        Fast in-process ElementFromPoint call (< 5 ms).
        Returns the primary interactive control (Button, MenuItem, Tab) or innermost control,
        automatically bypassing ghost drag handles (FrameGrabHandle) and activating Chromium web content.
        """
        try:
            ctrl = auto.ControlFromPoint(x, y)
            if ctrl is None:
                return None

            # Wake up Chromium accessibility tree if inspecting a Chrome/Edge/Electron window
            if ctrl.NativeWindowHandle:
                cls._wake_chromium_accessibility(ctrl.NativeWindowHandle)

            # If ctrl is a Chromium FrameGrabHandle, Intermediate D3D Window, or generic full-window pane:
            if getattr(ctrl, "ClassName", "") in ("FrameGrabHandle", "Intermediate D3D Window") or \
               (ctrl.ControlType == auto.ControlType.PaneControl and not ctrl.Name and not ctrl.AutomationId and (ctrl.BoundingRectangle.right - ctrl.BoundingRectangle.left) > 600):
                parent = ctrl.GetParentControl()
                if parent:
                    for sibling in parent.GetChildren():
                        if getattr(sibling, "ClassName", "") == "FrameGrabHandle":
                            continue
                        r = sibling.BoundingRectangle
                        if r.left <= x <= r.right and r.top <= y <= r.bottom:
                            best_ctrl = cls._drill_down(sibling, x, y)
                            return cls._control_to_element(best_ctrl)

            best_ctrl = cls._drill_down(ctrl, x, y)
            return cls._control_to_element(best_ctrl)
        except Exception as e:
            logger.debug("element_from_point failed at (%d, %d): %s", x, y, e)
            return None

    @classmethod
    def walk_subtree(
        cls,
        root_ctrl: Optional[auto.Control] = None,
        root_element: Optional[UIAElement] = None,
        max_depth: int = 20,
        max_elements: int = 250,
    ) -> List[UIAElement]:
        """
        BFS traversal of element subtree using UIA RawTreeWalker.
        Used for F4 Freeze Snapshot.
        """
        elements: List[UIAElement] = []
        target_ctrl = root_ctrl

        if target_ctrl is None and root_element and root_element.native_handle:
            try:
                target_ctrl = auto.ControlFromHandle(root_element.native_handle)
            except Exception:
                pass

        if target_ctrl is None:
            return elements

        queue = deque([(target_ctrl, 0)])
        while queue:
            if len(elements) >= max_elements:
                logger.debug("walk_subtree reached max_elements limit (%d)", max_elements)
                break

            curr, depth = queue.popleft()
            if depth > max_depth:
                continue

            el = cls._control_to_element(curr)
            if el:
                elements.append(el)

            child = curr.GetFirstChildControl()
            while child:
                queue.append((child, depth + 1))
                child = child.GetNextSiblingControl()

        return elements

    @classmethod
    def get_runtime_id_string(cls, ctrl: auto.Control) -> str:
        """Convert int array runtime ID [42, 12345, 0] to dot-separated string '42.12345.0'."""
        try:
            rt_id = ctrl.GetRuntimeId()
            if rt_id and isinstance(rt_id, (list, tuple)):
                return ".".join(map(str, rt_id))
        except Exception:
            pass
        return ""

    @classmethod
    def _control_to_element(cls, ctrl: auto.Control) -> Optional[UIAElement]:
        """Extract a detached UIAElement snapshot from a uiautomation Control."""
        try:
            # ControlType name: "ButtonControl" -> "Button"
            ct_name = ctrl.ControlTypeName
            if ct_name.endswith("Control"):
                ct_name = ct_name[:-7]

            rect_raw = ctrl.BoundingRectangle
            bounding_rect = None
            if rect_raw:
                bounding_rect = Rect(
                    left=rect_raw.left,
                    top=rect_raw.top,
                    right=rect_raw.right,
                    bottom=rect_raw.bottom
                )

            rt_id = cls.get_runtime_id_string(ctrl)

            return UIAElement(
                runtime_id=rt_id,
                control_type=ct_name or "Unknown",
                name=ctrl.Name or "",
                automation_id=ctrl.AutomationId or "",
                class_name=ctrl.ClassName or "",
                bounding_rect=bounding_rect,
                is_enabled=ctrl.IsEnabled,
                native_handle=ctrl.NativeWindowHandle or 0,
                help_text=getattr(ctrl, "HelpText", "") or ""
            )
        except Exception as e:
            logger.debug("Error converting control to element: %s", e)
            return None


