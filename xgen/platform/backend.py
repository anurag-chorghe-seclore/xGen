"""
PlatformBackend — the single interface every native-OS operation in xGen goes through.

One composite interface (not many small ones) by design: fewer files to maintain,
one obvious place to look for "what does xGen ask the OS to do." See
`xgen.platform.factory.get_platform_backend()` for how an implementation is chosen,
`xgen.platform.windows_backend.WindowsBackend` for the (only) real implementation,
and `xgen.platform.unsupported_backend.UnsupportedBackend` for the safe no-op used
on any non-Windows `sys.platform`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Protocol, Tuple

from xgen.utils.rect import Rect

if TYPE_CHECKING:
    # Avoids a runtime import cycle: xgen.utils.window_finder imports this
    # module's factory, so it must not need this module fully loaded back.
    from xgen.utils.window_finder import WindowTarget


@dataclass
class NativeElement:
    """
    Thread-safe, detached snapshot of a single native UI element, as reported
    by whatever accessibility API the current platform backend uses (UI
    Automation on Windows today).

    This is the same shape xGen has always called `UIAElement` — that name is
    kept as an alias in `xgen.core.uia_bridge` for backward compatibility.
    """
    runtime_id: str                      # e.g. "42.12345.0"
    control_type: str                    # "Button", "Edit", etc.
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    bounding_rect: Optional[Rect] = None
    is_enabled: bool = True
    native_handle: int = 0               # HWND (or platform equivalent) or 0
    help_text: str = ""
    aria_properties: str = ""


@dataclass
class TransientSnapshotResult:
    """Result of an F4 "capture the ephemeral element under the cursor" attempt."""
    status: str                          # "ok" | "no_control" | "desktop" | "unavailable" | "error"
    elements: List[NativeElement] = field(default_factory=list)
    message: str = ""


class PlatformBackend(Protocol):
    """
    Every native-OS operation xGen needs, grouped by concern but defined on one
    interface. A method's docstring here is normative: an implementation must
    match this behavior (including on failure) for callers not to need to know
    which backend they're talking to.
    """

    # --- Accessibility (native UIA / AX-equivalent) ---

    def initialize_accessibility(self) -> None:
        """One-time setup (e.g. COM init on Windows). Safe to call multiple times."""
        ...

    def is_accessibility_available(self) -> bool:
        """True if native element-under-cursor / subtree-walk queries can run at all."""
        ...

    def element_from_point(self, x: int, y: int) -> Optional[NativeElement]:
        """Fast in-process "what's the topmost interactive control at (x, y)". None if unavailable or nothing found."""
        ...

    def walk_subtree(
        self,
        root_element: Optional[NativeElement] = None,
        max_depth: int = 20,
        max_elements: int = 250,
    ) -> List[NativeElement]:
        """BFS walk of the native subtree rooted at root_element's window handle. Empty list if unavailable."""
        ...

    def capture_transient_snapshot(self, cursor_x: int, cursor_y: int) -> TransientSnapshotResult:
        """F4: find and walk the topmost ephemeral container (menu/tooltip/popup) under the cursor."""
        ...

    # --- Window enumeration / hit-testing ---

    def get_open_windows(self) -> List["WindowTarget"]:
        """Inspectable targets for the picker, most relevant first.

        What counts as a target differs by platform, which is what
        supports_desktop_root() below describes: on Windows these are top-level
        windows led by the Desktop Root pseudo-target; on macOS they are
        running applications, frontmost first, with no root entry at all.
        """
        ...

    def supports_desktop_root(self) -> bool:
        """Whether one session can span the whole desktop.

        True on Windows, where WinAppDriver's Root target inspects every
        application at once. False on macOS: an Appium Mac2 (XCUITest) session
        attaches to a single application, so offering a desktop-wide entry
        would promise something no Mac session can deliver.
        """
        ...

    def window_from_point(self, x: int, y: int) -> Optional[int]:
        """Native window handle at (x, y), or None."""
        ...

    def process_id_at_point(self, x: int, y: int) -> Optional[int]:
        """Owning process of whatever is on screen at (x, y), or None if unknown.

        Inspect Mode uses this to tell whether the thing under the cursor
        belongs to the application the session is attached to. Our own
        click-through overlay is never reported (see apply_click_through).
        """
        ...

    def get_process_id_for_window(self, hwnd: int) -> Optional[int]:
        """Owning process id for a window handle, or None."""
        ...

    def native_window_id_for_widget(self, widget: object) -> Optional[int]:
        """
        The id for a Qt widget's own window **in the same number space
        window_from_point() returns**, so a caller can tell "that's my own
        overlay" from "that's somebody else's window".

        Qt's winId() is only that id on Windows, where it is the HWND. On
        macOS winId() is an NSView pointer while window_from_point() returns a
        CGWindowID — comparing the two matches nothing, which silently breaks
        the overlay self-check and makes hover flicker. Ask this instead.
        """
        ...

    # --- Overlay window native styling ---

    def apply_click_through(self, widget: object) -> None:
        """Make a Qt widget click-through at the native window-manager level, if supported."""
        ...

    def enforce_topmost(self, widget: object) -> None:
        """Raise a Qt widget above other top-level windows (including menus), if supported."""
        ...

    # --- Display / DPI ---

    def init_dpi_awareness(self) -> None:
        """Opt the process into per-monitor DPI awareness before any UI is created. No-op if not applicable."""
        ...

    def uses_physical_pixel_coords(self) -> bool:
        """
        Whether this backend's screen coordinates are *physical pixels* that
        still need dividing by the display scale factor to reach Qt's logical
        space (True on Windows), or are already in that space (False on macOS,
        where both the accessibility API and the Appium Mac2 driver speak
        points).

        This is what keeps the three coordinate sources — native hit-testing,
        the driver's element rects, and Qt's own geometry — in one space. Get
        it wrong and nothing crashes: highlights land at half size and element
        matching quietly fails, because two of the three sources disagree by
        exactly the scale factor.
        """
        ...

    def get_physical_cursor_pos(self) -> Tuple[int, int]:
        """Current mouse cursor position in physical (not DPI-scaled) screen pixels."""
        ...

    def get_dpi_for_window(self, hwnd: int) -> int:
        """DPI for the given window handle. 96 (the OS-agnostic default) if unavailable."""
        ...

    # --- Privilege ---

    def is_running_as_admin(self) -> bool:
        """True if the current process has elevated/administrator privileges."""
        ...

    def relaunch_as_admin(self) -> bool:
        """Relaunch the current process elevated. True if the relaunch was initiated (process should exit after)."""
        ...

    # --- Cursor / app identity ---

    def move_cursor_to(self, x: int, y: int) -> bool:
        """Last-resort physical cursor move (used when a driver-side hover action isn't available)."""
        ...

    def set_app_user_model_id(self, app_id: str) -> None:
        """Set the OS-level app identity used for taskbar grouping/icon, if applicable. No-op if not."""
        ...

    def default_driver_platform(self) -> str:
        """
        Which Appium driver a *new* session should target by default on this
        machine: "windows" or "mac". Only a default — the user can point xGen
        at an Appium server running on the other OS, and the session's own
        capabilities always win (see xgen/core/driver_dialect.py). It lives
        here so that "what OS is this" stays behind the platform seam.
        """
        ...

    # --- Input suppression (xgen.capture.mouse_hook's native click-suppression hook) ---
    #
    # Windows' suppression wiring (pynput's win32_event_filter) predates these two
    # methods and is handled directly in mouse_hook.py without going through them —
    # they exist so a *second* backend (macOS today) can plug into the same pynput
    # suppression mechanism without mouse_hook.py ever importing a platform package
    # itself. Only meaningful for a backend whose kwarg uses "return the event to
    # pass it through, return something else to suppress it" semantics (pynput's
    # darwin_intercept shape) — see mouse_hook.py's _platform_intercept.

    def mouse_filter_kwarg_name(self) -> Optional[str]:
        """pynput mouse.Listener kwarg name for native click suppression on this OS
        (e.g. "darwin_intercept"), or None if this backend doesn't support one."""
        ...

    def decode_mouse_button_event(self, event_type: object, native_event: object) -> Optional[Tuple[int, int, bool]]:
        """Given this OS's raw suppression-callback arguments, return (x, y, is_press)
        for a left mouse button event in this backend's own screen-pixel convention,
        or None if the event isn't one xGen needs to act on."""
        ...
