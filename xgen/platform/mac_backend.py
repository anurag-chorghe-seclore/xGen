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

import contextlib
import logging
import os
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
    from AppKit import NSScreen, NSRunningApplication, NSThread, NSWorkspace
    HAS_APPKIT = True
except ImportError:
    NSScreen = None       # type: ignore
    NSRunningApplication = None  # type: ignore
    NSThread = None       # type: ignore
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
        # {id(widget): CGWindowID} for every window apply_click_through() has
        # made transparent to input. Recorded on the GUI thread at the moment
        # the window is shown, because window_from_point() runs on pynput's
        # listener thread, where touching AppKit is not allowed. See
        # _click_through_window_ids().
        self._click_through_ids: dict = {}

    def _click_through_window_ids(self) -> set:
        """CGWindowIDs of our own click-through windows, for hit-tests to skip."""
        return set(self._click_through_ids.values())

    @staticmethod
    def _autorelease_pool():
        """Give the calling thread an autorelease pool for the duration of a block.

        Every PyObjC call that returns an Objective-C object autoreleases it
        into the *current thread's* pool. The main thread always has one — the
        AppKit run loop drains it each pass — but a bare Python thread does
        not, so objects autoreleased there belong to no pool at all. On macOS
        that is not a leak, it is a crash: the eventual release lands against
        another thread's pool and the process dies with a segfault.

        This is exactly what killed the first live macOS CI run. xGen enumerates
        windows on a background thread (see MainWindow._refresh_window_picker,
        which moved that work off the UI thread to fix the Windows Refresh-button
        ANR, where EnumWindows can block on another app's stuck message loop).
        That was safe on Windows and fatal here. So every backend entry point
        that both touches PyObjC and can be reached from a non-GUI thread opens
        a pool first.
        """
        if HAS_OBJC and hasattr(objc, "autorelease_pool"):
            return objc.autorelease_pool()
        return contextlib.nullcontext()

    @staticmethod
    def _on_main_thread() -> bool:
        """Whether the caller is the main (AppKit) thread."""
        if not HAS_APPKIT:
            return True  # can't tell; assume the caller knows what it's doing
        try:
            return bool(NSThread.isMainThread())
        except Exception:
            return True

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
                bounding_rect = _rect_from_points(left, top, width, height)

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

    @staticmethod
    def _pid_for_element(element: object) -> Optional[int]:
        """Owning process of an AXUIElement, or None if it can't be determined."""
        if not HAS_AX or element is None:
            return None
        try:
            err, pid = AXFW.AXUIElementGetPid(element, None)
            if err != 0:
                return None
            return int(pid)
        except Exception:
            return None

    def _foreign_app_element_at(self, x: float, y: float) -> object:
        """Application AXUIElement of the topmost window at a point that isn't ours."""
        if not HAS_AX:
            return None
        own_pid = os.getpid()
        try:
            for info in self._windows_at_point(x, y):
                pid = int(info.get("kCGWindowOwnerPID", 0) or 0)
                if not pid or pid == own_pid:
                    continue
                return AXFW.AXUIElementCreateApplication(pid)
        except Exception as e:
            logger.debug("Could not resolve a foreign app element at (%s, %s): %s", x, y, e)
        return None

    def element_from_point(self, x: int, y: int) -> Optional[NativeElement]:
        if not HAS_AX:
            return None
        try:
            # The click path reaches this from the mouse-hook thread.
            with self._autorelease_pool():
                return self._element_from_point_pooled(float(x), float(y))
        except Exception as e:
            logger.debug("element_from_point failed at (%d, %d): %s", x, y, e)
            return None

    def _element_from_point_pooled(self, ax_x: float, ax_y: float) -> Optional[NativeElement]:
        """element_from_point's body, with an autorelease pool already open."""
        system_wide = AXFW.AXUIElementCreateSystemWide()
        err, element = AXFW.AXUIElementCopyElementAtPosition(system_wide, ax_x, ax_y, None)
        if err != 0 or element is None:
            return None

        # The highlight overlay sits at the maximum window level, right under
        # the cursor, and the accessibility hit-test honours window order
        # rather than setIgnoresMouseEvents_ — so without this the system-wide
        # hit-test keeps returning xGen's own overlay instead of the element it
        # is drawn around. Re-enter through the topmost window at that point
        # that belongs to another process.
        if self._pid_for_element(element) == os.getpid():
            element = self._foreign_app_element_at(ax_x, ax_y)
            if element is None:
                return None

        best = self._drill_down(element, ax_x, ax_y)
        return self._element_to_native(best)

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
            ax_x, ax_y = float(cursor_x), float(cursor_y)
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

    def supports_desktop_root(self) -> bool:
        """No macOS equivalent exists: a Mac2 session attaches to one app."""
        return False

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
            # Runs on a background thread (MainWindow._refresh_window_picker),
            # so it needs its own autorelease pool — see _autorelease_pool.
            with self._autorelease_pool():
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
        own_pid = os.getpid()
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
                pid=int(pid),
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

    def _windows_at_point(self, x: float, y: float) -> List[dict]:
        """Our own click-through windows excluded, front-to-back, at a point.

        CGWindowListCopyWindowInfo with kCGWindowListOptionOnScreenOnly returns
        windows topmost-first, so callers can take the first entry.
        """
        if not HAS_QUARTZ:
            return []
        skip = self._click_through_window_ids()
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        hits: List[dict] = []
        for info in window_list:
            if int(info.get("kCGWindowNumber", 0)) in skip:
                continue
            bounds = info.get("kCGWindowBounds") or {}
            left = bounds.get("X", 0)
            top = bounds.get("Y", 0)
            width = bounds.get("Width", 0)
            height = bounds.get("Height", 0)
            if left <= x <= left + width and top <= y <= top + height:
                hits.append(info)
        return hits

    def window_from_point(self, x: int, y: int) -> Optional[int]:
        if not HAS_QUARTZ:
            return None
        try:
            # Called from pynput's listener thread via the hover filter.
            with self._autorelease_pool():
                hits = self._windows_at_point(float(x), float(y))
                if not hits:
                    return None
                return int(hits[0].get("kCGWindowNumber", 0))
        except Exception:
            return None

    def process_id_at_point(self, x: int, y: int) -> Optional[int]:
        if not HAS_QUARTZ:
            return None
        try:
            with self._autorelease_pool():
                hits = self._windows_at_point(float(x), float(y))
                if not hits:
                    return None
                return int(hits[0].get("kCGWindowOwnerPID", 0)) or None
        except Exception:
            return None

    def get_process_id_for_window(self, hwnd: int) -> Optional[int]:
        if not HAS_QUARTZ or not hwnd:
            return None
        try:
            # Also reached from pynput's listener thread.
            with self._autorelease_pool():
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
        # winId() can *create* the native handle, which is an AppKit mutation,
        # and ns_view.window() is AppKit too. Neither is safe anywhere but the
        # main thread — off it, the result is a segfault rather than an
        # exception, so refuse instead of letting a caller find out the hard
        # way. Callers already treat None as "can't identify our own windows".
        if not MacBackend._on_main_thread():
            logger.debug("Refusing to resolve an NSWindow off the main thread.")
            return None

        # Only Qt's "cocoa" platform plugin backs a QWidget with a real NSView.
        # Under "offscreen" or "minimal" — how the test suite, CI and any
        # headless run work — there are no native Cocoa windows at all, and
        # winId() returns a placeholder handle that is not an Objective-C
        # object. Wrapping it hands PyObjC a bogus pointer to send messages to,
        # and the result is a segmentation fault, not an exception, so the
        # try/except below cannot catch it and the whole process dies. Check
        # the plugin rather than trusting winId() to mean on every platform
        # what it means on a real desktop.
        try:
            from PyQt6.QtGui import QGuiApplication
            app = QGuiApplication.instance()
            if app is None or app.platformName() != "cocoa":
                logger.debug("Qt platform plugin is not cocoa; no NSWindow to resolve.")
                return None
        except Exception as e:
            logger.debug("Could not determine the Qt platform plugin: %s", e)
            return None

        try:
            view_ptr = int(widget.winId())  # type: ignore[attr-defined]
            if not view_ptr:
                return None
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
        """Make the overlay's NSWindow ignore mouse events, so clicks pass through to whatever's underneath.

        Also records the window's CGWindowID so window_from_point() can skip
        it. setIgnoresMouseEvents_ only governs *event routing*: the window
        server's geometric hit-test still reports the window, and so does the
        accessibility hit-test. Since the highlight overlay sits at the maximum
        window level directly under the cursor, every unguarded hit-test lands
        on it rather than the element it is drawing around.

        Re-recorded on every call rather than once, because Qt can destroy and
        recreate the native NSWindow across a hide/show cycle, which changes
        the window number. A cached id that has gone stale is what makes the
        highlight blink: the filter stops recognising the overlay as its own,
        suppresses hover, hides the overlay, and the next tick shows it again.
        """
        ns_window = self._ns_window_for_widget(widget)
        if ns_window is None:
            return
        try:
            ns_window.setIgnoresMouseEvents_(True)
        except Exception as e:
            logger.debug("Could not apply native click-through: %s", e)
        try:
            self._click_through_ids[id(widget)] = int(ns_window.windowNumber())
        except Exception as e:
            logger.debug("Could not record click-through window id: %s", e)

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
                with self._autorelease_pool():
                    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
                    return int(round(loc.x)), int(round(loc.y))
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

    def uses_physical_pixel_coords(self) -> bool:
        # macOS reports points, not pixels, through both the accessibility API
        # and Appium's Mac2 driver — the same space Qt uses for geometry. No
        # scale conversion belongs anywhere in this backend's coordinates.
        return False

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
            x, y = int(round(loc.x)), int(round(loc.y))
            return (x, y, is_press)
        except Exception:
            return None


# ----------------------------------------------------------------------
# Coordinate space — points, all the way through
# ----------------------------------------------------------------------
#
# An earlier version of this backend multiplied every AX coordinate by the
# display's backing scale factor, to manufacture the "physical pixels"
# convention WindowsBackend naturally has, so that the existing
# physical->logical conversion in xgen/utils/dpi.py would keep working
# untouched. That reasoning only held while the native accessibility API was
# the single source of coordinates.
#
# It stopped holding the moment Appium's Mac2 driver joined: its element rects
# are in **points**, and nothing scales them. So the scaled AX values and the
# unscaled driver values sat exactly one scale factor apart, and on a Retina
# display ElementBridge compared numbers from two different spaces — every
# bounding-rect and point-containment match silently failed and fell through
# to the low-confidence synthetic fallback, while highlights derived from tree
# rects drew at half size. Nothing raised an error; it simply stopped
# recognising elements.
#
# macOS reports points from both sources, and points are the same space Qt
# uses for widget geometry — so the correct answer is to convert nowhere.
# uses_physical_pixel_coords() returns False here, which makes
# dpi.get_screen_dpr_at() report 1.0 on this platform and turns every
# conversion in the pipeline into the identity it should be.


def _rect_from_points(left: float, top: float, width: float, height: float) -> Rect:
    """Build a Rect from AX point values, with no scale conversion (see above)."""
    l = int(round(left))
    t = int(round(top))
    return Rect(left=l, top=t, right=l + int(round(width)), bottom=t + int(round(height)))
