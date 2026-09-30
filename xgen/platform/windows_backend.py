"""
WindowsBackend — the real PlatformBackend implementation.

Everything in this file is moved near-verbatim from where it used to live
(xgen/core/uia_bridge.py, xgen/utils/window_finder.py, xgen/utils/dpi.py,
xgen/utils/privilege.py, xgen/capture/overlay_window.py's native-styling
methods, xgen/core/driver_runner.py's cursor fallback, and
xgen/ui/main_window.py's _is_point_outside_xgen) — grouped under one class
instead of scattered across modules, with no intentional behavior change.

This module is only ever imported when xgen.platform.factory has already
decided sys.platform == "win32" (see factory.get_platform_backend()), so the
Windows-only imports below (uiautomation, comtypes, pywin32) are expected to
succeed. They're still wrapped defensively, matching the pattern already used
elsewhere in this codebase, in case an optional one (comtypes/pywin32) is
missing from an otherwise-Windows environment.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import sys
from collections import deque
from typing import List, Optional, Tuple

from xgen.platform.backend import NativeElement, TransientSnapshotResult
from xgen.utils.rect import Rect
from xgen.utils.window_finder import WindowTarget, _DESKTOP_ROOT

logger = logging.getLogger("xgen.platform.windows")

try:
    import uiautomation as auto
    HAS_UIAUTOMATION = True
except ImportError:
    auto = None             # type: ignore
    HAS_UIAUTOMATION = False

try:
    import comtypes
    from ctypes import wintypes as _wintypes
    HAS_COMTYPES = True
except ImportError:
    comtypes = None          # type: ignore
    _wintypes = None         # type: ignore
    HAS_COMTYPES = False

try:
    import win32api
    HAS_PYWIN32 = True
except ImportError:
    win32api = None          # type: ignore
    HAS_PYWIN32 = False

try:
    import win32gui
    import win32process
    HAS_WIN32_WINDOW_APIS = True
except ImportError:
    win32gui = None          # type: ignore
    win32process = None      # type: ignore
    HAS_WIN32_WINDOW_APIS = False


# --- Accessibility: native UIA constants (moved from uia_bridge.py) ---

_INTERACTIVE_CONTROL_TYPES = set()
if HAS_UIAUTOMATION:
    _INTERACTIVE_CONTROL_TYPES = {
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

_SPECIFIC_TRANSIENT_TYPES = {"Menu", "ToolTip", "Popup", "Flyout", "MenuItem"}


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WindowsBackend:
    """Concrete PlatformBackend for Windows 10, 11, and Windows Server."""

    # ------------------------------------------------------------------
    # Accessibility (moved from xgen/core/uia_bridge.py)
    # ------------------------------------------------------------------

    def initialize_accessibility(self) -> None:
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception as e:
            logger.debug("CoInitialize note: %s", e)

    def is_accessibility_available(self) -> bool:
        return HAS_UIAUTOMATION

    @classmethod
    def _wake_chromium_accessibility(cls, hwnd: int) -> None:
        """Activates Chromium's Blink accessibility engine on demand."""
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
    def _drill_down(cls, start_ctrl: "auto.Control", x: int, y: int) -> "auto.Control":
        """Descends into innermost child element at (x, y), stopping on interactive controls."""
        best = start_ctrl
        for _ in range(12):
            if best.ControlType in _INTERACTIVE_CONTROL_TYPES:
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

    def element_from_point(self, x: int, y: int) -> Optional[NativeElement]:
        if not HAS_UIAUTOMATION:
            return None
        try:
            ctrl = auto.ControlFromPoint(x, y)
            if ctrl is None:
                return None

            if ctrl.NativeWindowHandle:
                self._wake_chromium_accessibility(ctrl.NativeWindowHandle)

            if getattr(ctrl, "ClassName", "") in ("FrameGrabHandle", "Intermediate D3D Window") or \
               (ctrl.ControlType == auto.ControlType.PaneControl and not ctrl.Name and not ctrl.AutomationId and (ctrl.BoundingRectangle.right - ctrl.BoundingRectangle.left) > 600):
                parent = ctrl.GetParentControl()
                if parent:
                    for sibling in parent.GetChildren():
                        if getattr(sibling, "ClassName", "") == "FrameGrabHandle":
                            continue
                        r = sibling.BoundingRectangle
                        if r.left <= x <= r.right and r.top <= y <= r.bottom:
                            best_ctrl = self._drill_down(sibling, x, y)
                            return self._control_to_element(best_ctrl)

            best_ctrl = self._drill_down(ctrl, x, y)
            return self._control_to_element(best_ctrl)
        except Exception as e:
            logger.debug("element_from_point failed at (%d, %d): %s", x, y, e)
            return None

    def walk_subtree(
        self,
        root_element: Optional[NativeElement] = None,
        max_depth: int = 20,
        max_elements: int = 250,
    ) -> List[NativeElement]:
        """Public BFS walk, entered from a detached NativeElement (e.g. an auto-structure-change hook)."""
        if not HAS_UIAUTOMATION:
            return []
        target_ctrl = None
        if root_element and root_element.native_handle:
            try:
                target_ctrl = auto.ControlFromHandle(root_element.native_handle)
            except Exception:
                pass
        return self._walk_subtree_from_control(target_ctrl)

    def _walk_subtree_from_control(self, target_ctrl: Optional["auto.Control"]) -> List[NativeElement]:
        """Internal BFS walk starting from a live auto.Control (used for F4 snapshot's own root)."""
        elements: List[NativeElement] = []
        if target_ctrl is None:
            return elements

        queue = deque([(target_ctrl, 0)])
        while queue:
            if len(elements) >= 250:
                logger.debug("walk_subtree reached max_elements limit (250)")
                break

            curr, depth = queue.popleft()
            if depth > 20:
                continue

            el = self._control_to_element(curr)
            if el:
                elements.append(el)

            child = curr.GetFirstChildControl()
            while child:
                queue.append((child, depth + 1))
                child = child.GetNextSiblingControl()

        return elements

    @classmethod
    def get_runtime_id_string(cls, ctrl: "auto.Control") -> str:
        """Convert int array runtime ID [42, 12345, 0] to dot-separated string '42.12345.0'."""
        try:
            rt_id = ctrl.GetRuntimeId()
            if rt_id and isinstance(rt_id, (list, tuple)):
                return ".".join(map(str, rt_id))
        except Exception:
            pass
        return ""

    @classmethod
    def _control_to_element(cls, ctrl: "auto.Control") -> Optional[NativeElement]:
        """Extract a detached NativeElement snapshot from a uiautomation Control."""
        try:
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

            return NativeElement(
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

    # ------------------------------------------------------------------
    # F4 transient capture (moved from xgen/capture/transient_capture.py)
    # ------------------------------------------------------------------

    def capture_transient_snapshot(self, cursor_x: int, cursor_y: int) -> TransientSnapshotResult:
        if not HAS_UIAUTOMATION:
            return TransientSnapshotResult(
                status="unavailable",
                message="Live transient capture isn't available on this OS.",
            )
        try:
            ctrl = auto.ControlFromPoint(cursor_x, cursor_y)
            if ctrl is None:
                return TransientSnapshotResult(status="no_control", message="No control found under cursor.")

            if getattr(ctrl, "Name", "") == "Desktop" or getattr(ctrl, "ClassName", "") == "#32769":
                return TransientSnapshotResult(
                    status="desktop",
                    message="Cursor is over desktop background. Hover over an application element.",
                )

            # Walk up to the topmost transient container (popup window or menu)
            top_transient = ctrl
            curr = ctrl
            while curr:
                ct = curr.ControlTypeName.replace("Control", "")
                if ct in _SPECIFIC_TRANSIENT_TYPES:
                    top_transient = curr
                parent = curr.GetParentControl()
                if not parent or getattr(parent, "Name", "") == "Desktop":
                    class_name = getattr(curr, "ClassName", "")
                    if class_name in ("#32768", "tooltips_class32") or "Popup" in class_name or "DropDown" in class_name:
                        top_transient = curr
                    break
                parent_ct = parent.ControlTypeName.replace("Control", "")
                if parent_ct == "Window" and ct not in _SPECIFIC_TRANSIENT_TYPES:
                    break
                curr = parent

            # Walk entire transient subtree
            elements = self._walk_subtree_from_control(top_transient)
            if not elements:
                single_el = self._control_to_element(top_transient)
                if single_el:
                    elements = [single_el]

            if elements:
                return TransientSnapshotResult(status="ok", elements=elements)
            return TransientSnapshotResult(status="no_control", message="No UI elements found under cursor.")
        except Exception as e:
            logger.exception("F4 Freeze Snapshot error: %s", e)
            return TransientSnapshotResult(status="error", message=f"Capture failed: {e}")

    # ------------------------------------------------------------------
    # Window enumeration / hit-testing
    # (moved from xgen/utils/window_finder.py and xgen/ui/main_window.py's
    # _is_point_outside_xgen)
    # ------------------------------------------------------------------

    def get_open_windows(self) -> List[WindowTarget]:
        results: List[WindowTarget] = [_DESKTOP_ROOT]
        try:
            results.extend(self._enumerate_windows_win32())
        except Exception:
            logger.exception("Window enumeration failed; returning Desktop Root only.")
        return results

    @staticmethod
    def _get_exe_name(hwnd: int) -> str:
        """Resolve the basename of the executable owning this window."""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        pid = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.wintypes.DWORD(512)
        try:
            if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        except Exception:
            pass
        finally:
            kernel32.CloseHandle(h_proc)
        return ""

    # WM_GETTEXT plus SendMessageTimeoutW's ABORTIFHUNG flag, used by
    # _get_window_title_safe() below to bound the wait on one stuck window
    # (see Issue 1 / Refresh-button ANR for why this exists).
    _WM_GETTEXT = 0x000D
    _SMTO_ABORTIFHUNG = 0x0002
    _SMTO_BLOCK = 0x0001
    _WINDOW_TITLE_TIMEOUT_MS = 200

    @classmethod
    def _get_window_title_safe(cls, hwnd: int) -> Optional[str]:
        """Read a window's title without risking an unbounded block.

        GetWindowTextW()/GetWindowTextLengthW() send a cross-process WM_GETTEXT
        message and block with NO timeout if the target window's own message loop
        is busy or stuck — that unbounded wait, hit during EnumWindows on the UI
        thread, is exactly what caused the Refresh-button ANR (Issue 1). Calling
        SendMessageTimeoutW directly with SMTO_ABORTIFHUNG bounds the wait so one
        unresponsive window can never hang enumeration; a window that doesn't
        reply in time is treated as if it has no title (same as today's
        "if length == 0: skip" case for genuinely untitled windows).
        """
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(512)
        result = ctypes.wintypes.DWORD(0)
        replied = user32.SendMessageTimeoutW(
            hwnd, cls._WM_GETTEXT, 512, buf,
            cls._SMTO_ABORTIFHUNG | cls._SMTO_BLOCK, cls._WINDOW_TITLE_TIMEOUT_MS,
            ctypes.byref(result),
        )
        if not replied:
            return None
        return buf.value.strip()

    def _enumerate_windows_win32(self) -> List[WindowTarget]:
        """Walk all top-level windows via EnumWindows, keeping only visible app windows."""
        user32 = ctypes.windll.user32
        own_pid = os.getpid()
        found: List[WindowTarget] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )

        def _callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True

            title = self._get_window_title_safe(hwnd)
            if not title:
                return True

            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if (rect.right - rect.left) <= 0 or (rect.bottom - rect.top) <= 0:
                return True

            pid = ctypes.wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return True

            found.append(WindowTarget(
                title=title,
                handle_hex=hex(hwnd),
                hwnd=hwnd,
                exe_name=self._get_exe_name(hwnd),
            ))
            return True

        proc = EnumWindowsProc(_callback)
        user32.EnumWindows(proc, 0)
        found.sort(key=lambda w: w.title.lower())
        return found

    def window_from_point(self, x: int, y: int) -> Optional[int]:
        if not HAS_WIN32_WINDOW_APIS:
            return None
        try:
            return win32gui.WindowFromPoint((x, y))
        except Exception:
            return None

    def get_process_id_for_window(self, hwnd: int) -> Optional[int]:
        if not HAS_WIN32_WINDOW_APIS or not hwnd:
            return None
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Overlay native styling (moved from xgen/capture/overlay_window.py)
    # ------------------------------------------------------------------

    _GWL_EXSTYLE = -20
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_NOACTIVATE = 0x08000000
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOACTIVATE = 0x0010
    _SWP_SHOWWINDOW = 0x0040

    def apply_click_through(self, widget: object) -> None:
        """Apply Win32 WS_EX_TRANSPARENT safely without disrupting Qt alpha compositing."""
        try:
            hwnd = ctypes.wintypes.HWND(int(widget.winId()))  # type: ignore[attr-defined]
            user32 = ctypes.windll.user32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
            get_long.restype = ctypes.c_long
            set_long.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_long]
            set_long.restype = ctypes.c_long

            cur_style = get_long(hwnd, self._GWL_EXSTYLE)
            new_style = cur_style | self._WS_EX_TRANSPARENT | self._WS_EX_NOACTIVATE
            set_long(hwnd, self._GWL_EXSTYLE, new_style)
        except Exception as e:
            logger.debug("Could not apply native styles: %s", e)

    def enforce_topmost(self, widget: object) -> None:
        """Raise window to the top of the Z-band above context menus without overriding Qt DPI geometry."""
        try:
            hwnd = ctypes.wintypes.HWND(int(widget.winId()))  # type: ignore[attr-defined]
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.wintypes.UINT,
            ]
            user32.SetWindowPos.restype = ctypes.wintypes.BOOL
            hwnd_topmost = ctypes.wintypes.HWND(ctypes.c_void_p(-1).value)
            user32.SetWindowPos(
                hwnd,
                hwnd_topmost,
                0, 0, 0, 0,
                self._SWP_NOMOVE | self._SWP_NOSIZE | self._SWP_NOACTIVATE | self._SWP_SHOWWINDOW
            )
        except Exception as e:
            logger.debug("SetWindowPos topmost note: %s", e)

    # ------------------------------------------------------------------
    # Display / DPI (moved from xgen/utils/dpi.py)
    # ------------------------------------------------------------------

    def init_dpi_awareness(self) -> None:
        """Sets PROCESS_PER_MONITOR_DPI_AWARE_V2 (-4), falling back on older Windows versions."""
        try:
            user32 = ctypes.windll.user32
            if hasattr(user32, "SetProcessDpiAwarenessContext"):
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
                res = user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
                if res:
                    logger.debug("SetProcessDpiAwarenessContext(PER_MONITOR_V2) succeeded.")
                    return

            shcore = ctypes.windll.shcore
            if hasattr(shcore, "SetProcessDpiAwareness"):
                shcore.SetProcessDpiAwareness(2)
                logger.debug("SetProcessDpiAwareness(2) succeeded.")
                return

            user32.SetProcessDPIAware()
            logger.debug("SetProcessDPIAware() legacy succeeded.")
        except Exception as e:
            logger.warning("Failed to set DPI awareness: %s", e)

    def get_physical_cursor_pos(self) -> Tuple[int, int]:
        """Native GetCursorPos; works across Windows 7, 8, 10, 11."""
        try:
            pt = _Point()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        # Fall through to the same Qt-based fallback available on any platform.
        from xgen.utils.dpi import get_screen_dpr_at
        from PyQt6.QtGui import QCursor
        pos = QCursor.pos()
        dpr = get_screen_dpr_at(pos.x(), pos.y())
        return int(pos.x() * dpr), int(pos.y() * dpr)

    def get_dpi_for_window(self, hwnd: int) -> int:
        if not hwnd:
            return 96
        try:
            user32 = ctypes.windll.user32
            if hasattr(user32, "GetDpiForWindow"):
                dpi = user32.GetDpiForWindow(hwnd)
                if dpi > 0:
                    return int(dpi)
        except Exception:
            pass
        return 96

    # ------------------------------------------------------------------
    # Privilege (moved from xgen/utils/privilege.py)
    # ------------------------------------------------------------------

    def is_running_as_admin(self) -> bool:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def relaunch_as_admin(self) -> bool:
        if self.is_running_as_admin():
            logger.info("Already running with Administrator privileges.")
            return True
        try:
            executable = sys.executable
            params = " ".join([f'"{arg}"' for arg in sys.argv])
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                executable,
                params,
                None,
                1  # SW_SHOWNORMAL
            )
            if ret > 32:
                logger.info("Relaunch as administrator initiated. Exiting current process.")
                sys.exit(0)
                return True
            logger.warning("ShellExecuteW failed with error code: %d", ret)
            return False
        except Exception as e:
            logger.error("Failed to relaunch as administrator: %s", e)
            return False

    # ------------------------------------------------------------------
    # Cursor / app identity
    # ------------------------------------------------------------------

    def move_cursor_to(self, x: int, y: int) -> bool:
        """win32api.SetCursorPos fallback (moved from xgen/core/driver_runner.py)."""
        if not HAS_PYWIN32 or not win32api:
            return False
        try:
            win32api.SetCursorPos((x, y))
            return True
        except Exception:
            return False

    def set_app_user_model_id(self, app_id: str) -> None:
        """Moved from xgen/main.py — lets the taskbar group/icon the app correctly."""
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass
