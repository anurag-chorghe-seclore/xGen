"""
MacBackend — the macOS PlatformBackend implementation.

Mirrors xgen/platform/windows_backend.py: one composite class implementing
every PlatformBackend method, talking directly to Apple's native frameworks
(Accessibility / ApplicationServices, Quartz, AppKit) via PyObjC — the same
"talk to the OS's own SDK directly" philosophy WindowsBackend uses with
ctypes/pywin32/uiautomation, rather than depending on a third-party
automation wrapper (e.g. the unmaintained `atomacos`).

*** UNVERIFIED ON REAL macOS — this was written from documented Apple/PyObjC
*** API shapes alone, in a Linux sandbox with no Mac available. Treat every
*** method here as a first draft until it has been exercised on an actual
*** Mac (Sequoia/Tahoe), the same way Phase 3 of the Windows-guard work
*** found four real bugs that code inspection alone had missed. See the
*** "Open questions / known risk areas" note block at the bottom of this
*** file (and the project doc this was written alongside) for the specific
*** things most likely to need a fix once tested live.

Requires three macOS permission grants the user must approve once in
System Settings > Privacy & Security, with no programmatic auto-elevation
possible (unlike Windows' UAC relaunch):
  - Accessibility          -> required for every AXUIElement* call below.
  - Input Monitoring       -> required for pynput's global mouse/keyboard
                               hooks (capture/mouse_hook.py, keyboard_hook.py)
                               to receive events system-wide.
  - Screen Recording       -> required (since macOS Catalina) for
                               CGWindowListCopyWindowInfo to report *other
                               apps'* window titles; without it, get_open_windows()
                               still lists windows but kCGWindowName comes back
                               empty, degrading (not crashing) the Connect
                               dialog's window picker to "owner app name only".

A coordinate-space note, because it's the single easiest thing to get wrong
here: every native-AX-sourced coordinate (cursor position, element bounding
rects) is expressed by Apple's APIs in *points* (already DPI-independent —
there is no separate "physical pixel" concept exposed at this layer the way
Win32's GetCursorPos/BoundingRectangle are physical pixels). But the rest of
xGen's pipeline (xgen/utils/dpi.py's physical_to_logical_rect, and
main_window.py's _is_point_outside_xgen) was written for Windows' convention
and unconditionally *divides* whatever this backend returns by
QScreen.devicePixelRatio() to get Qt logical coordinates. Left alone, that
would silently halve every highlight box and hit-test on a Retina display.
To avoid touching that already-verified Windows pipeline, this backend
instead *multiplies* its native point-space values by the local backing
scale factor before returning them — i.e. it manufactures the same
"pseudo-physical" pixel convention Windows naturally has, so every existing
call site keeps working unmodified. See _to_pseudo_physical()/_to_ax_points().
"""

from __future__ import annotations

import logging
import subprocess
from collections import deque
from typing import List, Optional, Tuple

from xgen.platform.backend import NativeElement, TransientSnapshotResult
from xgen.utils.rect import Rect
from xgen.utils.window_finder import WindowTarget

logger = logging.getLogger("xgen.platform.mac")

# --- Guarded PyObjC imports (mirrors windows_backend.py's HAS_* pattern) ---
# Each framework is a separate pyobjc wheel; a partial install should degrade
# the relevant methods gracefully rather than crash the whole backend.

try:
    import objc  # core PyObjC bridge: lets us turn a Qt NSView pointer into an NSObject
    HAS_OBJC = True
except ImportError:
    objc = None  # type: ignore
    HAS_OBJC = False

try:
    import ApplicationServices as AXFW
    HAS_APPLICATIONSERVICES = True
except ImportError:
    AXFW = None  # type: ignore
    HAS_APPLICATIONSERVICES = False

try:
    import Quartz
    HAS_QUARTZ = True
except ImportError:
    Quartz = None  # type: ignore
    HAS_QUARTZ = False

try:
    from AppKit import NSScreen, NSRunningApplication, NSWorkspace
    HAS_APPKIT = True
except ImportError:
    NSScreen = None       # type: ignore
    NSRunningApplication = None  # type: ignore
    NSWorkspace = None    # type: ignore
    HAS_APPKIT = False

HAS_AX = HAS_OBJC and HAS_APPLICATIONSERVICES and HAS_QUARTZ and HAS_APPKIT


# --- Accessibility: AX role / attribute constants ---
#
# Hardcoded as the string/int literals Apple documents (rather than imported
# names like AXFW.kAXRoleAttribute), because the exact Python symbol names
# PyObjC exposes have drifted across versions in the past; the underlying
# constants themselves (plain CFString role/attribute names, and the
# AXValueType enum ints from <HIServices/AXValue.h>) are stable public API.
_AX_ROLE = "AXRole"
_AX_SUBROLE = "AXSubrole"
_AX_TITLE = "AXTitle"
_AX_DESCRIPTION = "AXDescription"
_AX_VALUE = "AXValue"
_AX_IDENTIFIER = "AXIdentifier"
_AX_HELP = "AXHelp"
_AX_ENABLED = "AXEnabled"
_AX_POSITION = "AXPosition"
_AX_SIZE = "AXSize"
_AX_CHILDREN = "AXChildren"
_AX_PARENT = "AXParent"
_AX_VALUE_TYPE_CGPOINT = 1   # kAXValueCGPointType
_AX_VALUE_TYPE_CGSIZE = 2    # kAXValueCGSizeType

# Roles treated as "stop drilling down, this is an actionable control" —
# the Mac analog of windows_backend.py's _INTERACTIVE_CONTROL_TYPES.
_INTERACTIVE_ROLES = {
    "AXButton", "AXMenuItem", "AXMenuButton", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXComboBox", "AXTextField", "AXTextArea", "AXLink",
    "AXSlider", "AXStepper", "AXTabGroup", "AXRow", "AXCell", "AXDisclosureTriangle",
}

# Roles treated as an ephemeral/transient container for F4 capture — the Mac
# analog of windows_backend.py's _SPECIFIC_TRANSIENT_TYPES.
_TRANSIENT_ROLES = {"AXMenu", "AXMenuItem", "AXPopover", "AXSheet", "AXDrawer"}

# AXRole -> the control_type this backend reports.
#
# These deliberately use XCUITest's XCUIElementType* vocabulary rather than
# the Windows UIA names, because the Appium Mac2 driver's page source tags
# elements that way (see xgen/core/driver_dialect.py). ElementBridge matches
# this live-AX path against the Appium-XML-sourced tree by comparing those
# strings, so the two must agree — a hover resolving to "Button" would never
# line up with an XML node tagged XCUIElementTypeButton.
#
# Most AX roles map by rule (AXButton -> XCUIElementTypeButton); only the
# handful where the two frameworks genuinely disagree are listed here.
_ROLE_TO_CONTROL_TYPE = {
    "AXTextArea": "XCUIElementTypeTextView",
    "AXMenuButton": "XCUIElementTypePopUpButton",
    "AXList": "XCUIElementTypeList",
    "AXOutline": "XCUIElementTypeOutline",
    "AXGrid": "XCUIElementTypeCollectionView",
}


class MacBackend:
    """Concrete PlatformBackend for macOS (Sequoia, Tahoe, and later)."""

    def __init__(self) -> None:
        # Widgets whose NSWindow level has already been raised; see enforce_topmost.
        self._topmost_applied: set = set()

    # ------------------------------------------------------------------
    # Accessibility
    # ------------------------------------------------------------------

    def initialize_accessibility(self) -> None:
        # No native init call is needed before using AXUIElement* (unlike
        # Windows' CoInitialize for COM) — kept as a no-op for interface parity.
        pass

    def is_accessibility_available(self) -> bool:
        if not HAS_AX:
            return False
        try:
            return bool(AXFW.AXIsProcessTrusted())
        except Exception:
            return False

    @staticmethod
    def _ax_attr(element: object, attr: str, default: object = None) -> object:
        if not HAS_AX:
            return default
        try:
            err, value = AXFW.AXUIElementCopyAttributeValue(element, attr, None)
            if err != 0:
                return default
            return value
        except Exception:
            return default

    @classmethod
    def _ax_point_size(cls, element: object) -> Optional[Tuple[float, float, float, float]]:
        """Returns (left, top, width, height) in native AX points, or None."""
        pos_val = cls._ax_attr(element, _AX_POSITION)
        size_val = cls._ax_attr(element, _AX_SIZE)
        if pos_val is None or size_val is None:
            return None
        try:
            ok1, point = AXFW.AXValueGetValue(pos_val, _AX_VALUE_TYPE_CGPOINT, None)
            ok2, size = AXFW.AXValueGetValue(size_val, _AX_VALUE_TYPE_CGSIZE, None)
            if not ok1 or not ok2:
                return None
            return (float(point.x), float(point.y), float(size.width), float(size.height))
        except Exception:
            return None

    @classmethod
    def _drill_down(cls, start_element: object, ax_x: float, ax_y: float) -> object:
        """Descends into the innermost child element at (ax_x, ax_y), stopping on interactive roles."""
        best = start_element
        for _ in range(12):
            role = cls._ax_attr(best, _AX_ROLE, "") or ""
            if role in _INTERACTIVE_ROLES:
                break
            children = cls._ax_attr(best, _AX_CHILDREN, []) or []
            found = False
            best_box = cls._ax_point_size(best)
            best_area = (best_box[2] * best_box[3]) if best_box else None
            for child in children:
                box = cls._ax_point_size(child)
                if not box:
                    continue
                cl, ct, cw, ch = box
                if cl <= ax_x <= cl + cw and ct <= ax_y <= ct + ch:
                    child_area = cw * ch
                    if child_area > 0 and (best_area is None or child_area < best_area):
                        best = child
                        found = True
                        break
            if not found:
                break
        return best

    @classmethod
    def _to_control_type(cls, role: str) -> str:
        """Map an AX role to the XCUIElementType name Mac2Driver would use."""
        if not role:
            return "Unknown"
        mapped = _ROLE_TO_CONTROL_TYPE.get(role)
        if mapped:
            return mapped
        if role.startswith("AX") and len(role) > 2:
            return f"XCUIElementType{role[2:]}"
        return role

    @classmethod
    def _element_to_native(cls, element: object) -> Optional[NativeElement]:
        if element is None:
            return None
        try:
            role = cls._ax_attr(element, _AX_ROLE, "") or ""
            title = cls._ax_attr(element, _AX_TITLE, "") or ""
            if not title:
                desc = cls._ax_attr(element, _AX_DESCRIPTION, "") or ""
                title = desc or (cls._ax_attr(element, _AX_VALUE, "") or "")
                if not isinstance(title, str):
                    title = ""
            identifier = cls._ax_attr(element, _AX_IDENTIFIER, "") or ""
            help_text = cls._ax_attr(element, _AX_HELP, "") or ""
            enabled = cls._ax_attr(element, _AX_ENABLED, True)

            bounding_rect = None
            box = cls._ax_point_size(element)
            if box:
                left, top, width, height = box
                bounding_rect = _PSEUDO_PHYSICAL.rect_from_points(left, top, width, height)

            return NativeElement(
                runtime_id="",  # AX has no UIA-RuntimeId equivalent; see module docstring / design note.
                control_type=cls._to_control_type(role),
                name=str(title) if title else "",
                automation_id=str(identifier) if identifier else "",
                class_name=role,  # closest analog available: the raw AX role (e.g. "AXButton")
                bounding_rect=bounding_rect,
                is_enabled=bool(enabled) if isinstance(enabled, bool) else True,
                native_handle=0,
                help_text=str(help_text) if help_text else "",
            )
        except Exception as e:
            logger.debug("Error converting AX element to NativeElement: %s", e)
            return None

    def element_from_point(self, x: int, y: int) -> Optional[NativeElement]:
        if not HAS_AX:
            return None
        try:
            ax_x, ax_y = _PSEUDO_PHYSICAL.to_points(x, y)
            system_wide = AXFW.AXUIElementCreateSystemWide()
            err, element = AXFW.AXUIElementCopyElementAtPosition(system_wide, ax_x, ax_y, None)
            if err != 0 or element is None:
                return None
            best = self._drill_down(element, ax_x, ax_y)
            return self._element_to_native(best)
        except Exception as e:
            logger.debug("element_from_point failed at (%d, %d): %s", x, y, e)
            return None

    def walk_subtree(
        self,
        root_element: Optional[NativeElement] = None,
        max_depth: int = 20,
        max_elements: int = 250,
    ) -> List[NativeElement]:
        # KNOWN GAP (Phase M1): on Windows, this re-enters a *window-level*
        # element via its raw HWND (auto.ControlFromHandle). AXUIElementRef
        # has no equivalent "reconstruct from an opaque integer" operation,
        # and NativeElement.native_handle is always 0 on this backend (see
        # _element_to_native above), so there is currently no live AXUIElement
        # to resume walking from. In practice this is only reachable via
        # transient_capture.py's "Mechanism 3" (on_structure_changed), which
        # isn't wired to a live hook on either platform today — returning []
        # here is honest about that rather than faking a fragile heuristic.
        return []

    def capture_transient_snapshot(self, cursor_x: int, cursor_y: int) -> TransientSnapshotResult:
        if not HAS_AX:
            return TransientSnapshotResult(
                status="unavailable",
                message="Live transient capture isn't available (PyObjC Accessibility frameworks not found).",
            )
        if not self.is_accessibility_available():
            return TransientSnapshotResult(
                status="unavailable",
                message="xGen needs Accessibility permission (System Settings > Privacy & Security > Accessibility).",
            )
        try:
            ax_x, ax_y = _PSEUDO_PHYSICAL.to_points(cursor_x, cursor_y)
            system_wide = AXFW.AXUIElementCreateSystemWide()
            err, element = AXFW.AXUIElementCopyElementAtPosition(system_wide, ax_x, ax_y, None)
            if err != 0 or element is None:
                return TransientSnapshotResult(status="no_control", message="No control found under cursor.")

            role = self._ax_attr(element, _AX_ROLE, "") or ""
            if role in ("AXApplication",) and not self._ax_attr(element, _AX_CHILDREN):
                return TransientSnapshotResult(
                    status="desktop",
                    message="Cursor is over desktop background. Hover over an application element.",
                )

            # Walk up to the topmost transient container (menu/popover/sheet), mirroring
            # WindowsBackend.capture_transient_snapshot's ancestor walk.
            top_transient = element
            curr = element
            depth_guard = 0
            while curr is not None and depth_guard < 50:
                depth_guard += 1
                curr_role = self._ax_attr(curr, _AX_ROLE, "") or ""
                if curr_role in _TRANSIENT_ROLES:
                    top_transient = curr
                parent = self._ax_attr(curr, _AX_PARENT)
                if parent is None:
                    break
                parent_role = self._ax_attr(parent, _AX_ROLE, "") or ""
                if parent_role == "AXWindow" and curr_role not in _TRANSIENT_ROLES:
                    break
                curr = parent

            elements = self._walk_subtree_from_element(top_transient, max_depth=20, max_elements=250)
            if not elements:
                single_el = self._element_to_native(top_transient)
                if single_el:
                    elements = [single_el]

            if elements:
                return TransientSnapshotResult(status="ok", elements=elements)
            return TransientSnapshotResult(status="no_control", message="No UI elements found under cursor.")
        except Exception as e:
            logger.exception("F4 Freeze Snapshot error: %s", e)
            return TransientSnapshotResult(status="error", message=f"Capture failed: {e}")

    def _walk_subtree_from_element(self, root: object, max_depth: int = 20, max_elements: int = 250) -> List[NativeElement]:
        """Internal BFS walk starting from a live AXUIElement (used for F4's own root)."""
        elements: List[NativeElement] = []
        if root is None:
            return elements

        queue = deque([(root, 0)])
        while queue:
            if len(elements) >= max_elements:
                logger.debug("walk_subtree reached max_elements limit (%d)", max_elements)
                break
            curr, depth = queue.popleft()
            if depth > max_depth:
                continue
            el = self._element_to_native(curr)
            if el:
                elements.append(el)
            children = self._ax_attr(curr, _AX_CHILDREN, []) or []
            for child in children:
                queue.append((child, depth + 1))
        return elements

    # ------------------------------------------------------------------
    # Window enumeration / hit-testing
    # ------------------------------------------------------------------

    def get_open_windows(self) -> List[WindowTarget]:
        """Running *applications*, frontmost first — not windows, and no Desktop Root.

        This deliberately differs from WindowsBackend. Appium's Mac2 driver
        (XCUITest) attaches to one application at a time; macOS has no
        desktop-wide target, so offering a "Desktop Root (Entire OS)" entry
        here would promise something no Mac session can deliver — and picking
        it would silently land the user on Finder, which is Mac2Driver's own
        default. Windows are still what we enumerate underneath (that's what
        the window server exposes), but they're collapsed to one entry per
        owning app, because that is the unit a session can actually target.
        """
        if not HAS_QUARTZ:
            return []
        try:
            return self._enumerate_applications_cg()
        except Exception:
            logger.exception("Application enumeration failed; returning an empty list.")
            return []

    def _enumerate_applications_cg(self) -> List[WindowTarget]:
        """Collapse the on-screen window list into one entry per owning application.

        Order is the window server's own front-to-back order, so index 0 is the
        frontmost app — which is what a new session defaults to.

        NOTE: since macOS Catalina, kCGWindowName for windows owned by OTHER
        processes is only populated if this process has been granted Screen
        Recording permission. Without it the apps still appear here (the list
        is never empty) because the owning app's name comes from
        kCGWindowOwnerName, which needs no permission — only per-window titles
        are lost, and those aren't what a Mac session targets anyway.
        """
        own_pid = __import__("os").getpid()
        found: List[WindowTarget] = []
        seen_apps: set = set()

        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []

        for info in window_list:
            layer = info.get("kCGWindowLayer", 0)
            if layer != 0:
                continue  # skip menu bar, dock, desktop icons, etc. (normal app windows are layer 0)

            pid = info.get("kCGWindowOwnerPID", 0)
            if pid == own_pid:
                continue

            bounds = info.get("kCGWindowBounds") or {}
            if bounds.get("Width", 0) <= 0 or bounds.get("Height", 0) <= 0:
                continue

            owner_name = info.get("kCGWindowOwnerName", "") or ""
            bundle_id = self._bundle_id_for_pid(pid)
            app_key = bundle_id or owner_name
            if not app_key or app_key in seen_apps:
                continue
            seen_apps.add(app_key)

            window_id = info.get("kCGWindowNumber", 0)
            found.append(WindowTarget(
                title=owner_name or bundle_id,
                handle_hex=hex(window_id),
                hwnd=window_id,
                # Shown after the app name in the picker, so the user sees the
                # identifier the session will actually be started with.
                exe_name=bundle_id,
                bundle_id=bundle_id,
            ))

        return found

    @staticmethod
    def _bundle_id_for_pid(pid: int) -> str:
        """Bundle identifier of the app owning a pid — what a Mac2 session targets."""
        if not HAS_APPKIT or not pid:
            return ""
        try:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            if app is None:
                return ""
            return str(app.bundleIdentifier() or "")
        except Exception:
            return ""

    def window_from_point(self, x: int, y: int) -> Optional[int]:
        if not HAS_QUARTZ:
            return None
        try:
            ax_x, ax_y = _PSEUDO_PHYSICAL.to_points(x, y)
            options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
            # CGWindowListCopyWindowInfo with this option set returns windows
            # front-to-back (topmost first), so the first bounds match wins.
            for info in window_list:
                bounds = info.get("kCGWindowBounds") or {}
                left = bounds.get("X", 0)
                top = bounds.get("Y", 0)
                width = bounds.get("Width", 0)
                height = bounds.get("Height", 0)
                if left <= ax_x <= left + width and top <= ax_y <= top + height:
                    return int(info.get("kCGWindowNumber", 0))
            return None
        except Exception:
            return None

    def get_process_id_for_window(self, hwnd: int) -> Optional[int]:
        if not HAS_QUARTZ or not hwnd:
            return None
        try:
            desc = Quartz.CGWindowListCreateDescriptionFromArray([hwnd]) or []
            if desc:
                return int(desc[0].get("kCGWindowOwnerPID", 0)) or None
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Overlay native styling
    # ------------------------------------------------------------------

    @staticmethod
    def _ns_window_for_widget(widget: object) -> Optional[object]:
        """Bridge a Qt widget's native NSView (via winId()) to its owning NSWindow.

        UNVERIFIED: objc.objc_object(c_void_p=...) on a PyQt6 winId() pointer
        is the idiom several existing Qt+PyObjC integrations use to reach a
        widget's native NSWindow, but it has not been exercised against this
        codebase's actual PyQt6 version — confirm on first live run.
        """
        if not HAS_OBJC:
            return None
        try:
            view_ptr = int(widget.winId())  # type: ignore[attr-defined]
            ns_view = objc.objc_object(c_void_p=view_ptr)
            return ns_view.window()
        except Exception as e:
            logger.debug("Could not resolve NSWindow for widget: %s", e)
            return None

    def native_window_id_for_widget(self, widget: object) -> Optional[int]:
        """NSWindow.windowNumber() — which *is* the CGWindowID window_from_point returns."""
        ns_window = self._ns_window_for_widget(widget)
        if ns_window is None:
            return None
        try:
            return int(ns_window.windowNumber())
        except Exception:
            return None

    def apply_click_through(self, widget: object) -> None:
        """Make the overlay's NSWindow ignore mouse events, so clicks pass through to whatever's underneath."""
        ns_window = self._ns_window_for_widget(widget)
        if ns_window is None:
            return
        try:
            ns_window.setIgnoresMouseEvents_(True)
        except Exception as e:
            logger.debug("Could not apply native click-through: %s", e)

    def enforce_topmost(self, widget: object) -> None:
        """Raise the overlay's NSWindow above other windows, including context menus.

        Called on every hover tick (~10/s) by OverlayWindow.move_to(). On
        Windows SetWindowPos that often is harmless, but re-ordering an
        NSWindow ten times a second makes the highlight visibly flicker, so
        the ordering work is done once per window: the overlay already sits at
        the maximum window level, which keeps it on top without re-raising.
        """
        try:
            key = id(widget)
            if key in self._topmost_applied:
                return
            ns_window = self._ns_window_for_widget(widget)
            if ns_window is None:
                return
            if HAS_QUARTZ:
                level = Quartz.CGWindowLevelForKey(Quartz.kCGMaximumWindowLevelKey)
                ns_window.setLevel_(level)
            ns_window.orderFrontRegardless()
            self._topmost_applied.add(key)
        except Exception as e:
            logger.debug("Could not enforce topmost: %s", e)

    # ------------------------------------------------------------------
    # Display / DPI
    # ------------------------------------------------------------------

    def init_dpi_awareness(self) -> None:
        # macOS apps are Retina-aware automatically once the app bundle's
        # Info.plist sets NSHighResolutionCapable (a packaging-time concern,
        # tracked under the macOS-packaging phase) — no per-process runtime
        # opt-in call exists the way Windows needs SetProcessDpiAwarenessContext.
        pass

    @staticmethod
    def _backing_scale_factor_at(x_pts: float, y_pts: float) -> float:
        """Backing scale factor (1.0 or 2.0 on Retina) of the NSScreen containing (x_pts, y_pts), in AX point space."""
        if not HAS_APPKIT:
            return 1.0
        try:
            for screen in NSScreen.screens():
                frame = screen.frame()
                ox, oy = frame.origin.x, frame.origin.y
                w, h = frame.size.width, frame.size.height
                # NSScreen frames use Cocoa's bottom-left origin; AX/CG points use
                # top-left origin. Converting precisely requires the main screen's
                # height as a reference, which NSScreen.screens()[0] provides.
                main_h = NSScreen.screens()[0].frame().size.height
                top = main_h - (oy + h)
                if ox <= x_pts <= ox + w and top <= y_pts <= top + h:
                    return float(screen.backingScaleFactor())
            return float(NSScreen.mainScreen().backingScaleFactor()) if NSScreen.mainScreen() else 1.0
        except Exception:
            return 1.0

    def get_physical_cursor_pos(self) -> Tuple[int, int]:
        if HAS_QUARTZ:
            try:
                loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
                return _PSEUDO_PHYSICAL.to_pseudo_physical(float(loc.x), float(loc.y))
            except Exception:
                pass
        # Fall through to the same Qt-based fallback WindowsBackend uses when the native call fails.
        from xgen.utils.dpi import get_screen_dpr_at
        from PyQt6.QtGui import QCursor
        pos = QCursor.pos()
        dpr = get_screen_dpr_at(pos.x(), pos.y())
        return int(pos.x() * dpr), int(pos.y() * dpr)

    def get_dpi_for_window(self, hwnd: int) -> int:
        if not hwnd or not HAS_QUARTZ:
            return 96
        try:
            desc = Quartz.CGWindowListCreateDescriptionFromArray([hwnd]) or []
            if not desc:
                return 96
            bounds = desc[0].get("kCGWindowBounds") or {}
            cx = bounds.get("X", 0) + bounds.get("Width", 0) / 2
            cy = bounds.get("Y", 0) + bounds.get("Height", 0) / 2
            return int(96 * self._backing_scale_factor_at(cx, cy))
        except Exception:
            return 96

    # ------------------------------------------------------------------
    # Privilege
    # ------------------------------------------------------------------
    #
    # macOS has no UAC-style runtime elevation: there is nothing to "relaunch
    # as admin" into. These two methods are repurposed to the closest macOS
    # analog — Accessibility trust, the one permission every AX call in this
    # file depends on — so existing callers (xgen/utils/privilege.py and
    # whatever UI surfaces them) keep a meaningful answer instead of a
    # hardcoded False. FLAG FOR REVIEW: audit wherever privilege.py's
    # is_running_as_admin()/relaunch_as_admin() results are shown in the UI
    # (e.g. a toolbar/menu "Run as Administrator" action) and update the
    # copy for Mac, since "elevate" there should read as "open System
    # Settings and grant Accessibility," not a relaunch.

    def is_running_as_admin(self) -> bool:
        return self.is_accessibility_available()

    def relaunch_as_admin(self) -> bool:
        """Opens System Settings' Accessibility pane instead of relaunching (no Mac equivalent of UAC exists)."""
        try:
            subprocess.Popen([
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ])
        except Exception as e:
            logger.debug("Could not open Accessibility settings pane: %s", e)
        return False  # never "initiates a relaunch" on Mac; caller should not exit()

    # ------------------------------------------------------------------
    # Cursor / app identity
    # ------------------------------------------------------------------

    def move_cursor_to(self, x: int, y: int) -> bool:
        """CGWarpMouseCursorPosition fallback. x, y are treated as native CG/AX points
        (consistent with coordinates Appium's Mac2Driver/XCUITest reports in element
        rects), NOT this backend's own pseudo-physical convention — see move_cursor_to's
        one caller, DriverRunner.hover_xpath, which sources x, y from the driver's own
        /rect response, not from element_from_point/get_physical_cursor_pos above."""
        if not HAS_QUARTZ:
            return False
        try:
            Quartz.CGWarpMouseCursorPosition((float(x), float(y)))
            return True
        except Exception:
            return False

    def set_app_user_model_id(self, app_id: str) -> None:
        # No runtime equivalent: Dock grouping/icon identity on macOS comes
        # from the .app bundle's Info.plist (CFBundleIdentifier), set at
        # packaging time, not via a process API call. No-op by design.
        pass

    def default_driver_platform(self) -> str:
        return "mac"

    # ------------------------------------------------------------------
    # Input suppression (see backend.py's PlatformBackend docstring for why
    # this exists — it lets capture/mouse_hook.py plug macOS's native click
    # suppression in without ever importing Quartz itself)
    # ------------------------------------------------------------------

    def mouse_filter_kwarg_name(self) -> Optional[str]:
        return "darwin_intercept" if HAS_QUARTZ else None

    def decode_mouse_button_event(self, event_type: object, native_event: object) -> Optional[Tuple[int, int, bool]]:
        if not HAS_QUARTZ:
            return None
        try:
            if event_type == Quartz.kCGEventLeftMouseDown:
                is_press = True
            elif event_type == Quartz.kCGEventLeftMouseUp:
                is_press = False
            else:
                return None
            loc = Quartz.CGEventGetLocation(native_event)
            x, y = _PSEUDO_PHYSICAL.to_pseudo_physical(float(loc.x), float(loc.y))
            return (x, y, is_press)
        except Exception:
            return None


# ----------------------------------------------------------------------
# Coordinate-space normalization (see module docstring)
# ----------------------------------------------------------------------

class _PseudoPhysicalCoords:
    """
    Converts between native AX "points" and the "pseudo-physical" pixel
    convention the rest of xGen's pipeline expects (see module docstring).
    A tiny stateless helper, not a MacBackend method, so it can be unit
    tested without any PyObjC import succeeding.
    """

    @staticmethod
    def to_pseudo_physical(x_pts: float, y_pts: float) -> Tuple[int, int]:
        dpr = MacBackend._backing_scale_factor_at(x_pts, y_pts)
        return int(round(x_pts * dpr)), int(round(y_pts * dpr))

    @staticmethod
    def to_points(x_phys: int, y_phys: int) -> Tuple[float, float]:
        # Scale factor is looked up at the *physical* point first using a 1.0
        # guess, then refined — in practice screens are rarely mixed-DPI at
        # adjacent pixels, so a single pass (assume 1.0, look up, rescale) is
        # accurate enough; exact only matters at display-boundary pixels.
        dpr = MacBackend._backing_scale_factor_at(x_phys, y_phys)
        return x_phys / dpr, y_phys / dpr

    @staticmethod
    def rect_from_points(left: float, top: float, width: float, height: float) -> Rect:
        dpr = MacBackend._backing_scale_factor_at(left, top)
        l = int(round(left * dpr))
        t = int(round(top * dpr))
        w = int(round(width * dpr))
        h = int(round(height * dpr))
        return Rect(left=l, top=t, right=l + w, bottom=t + h)


_PSEUDO_PHYSICAL = _PseudoPhysicalCoords()
