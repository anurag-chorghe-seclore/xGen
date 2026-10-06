"""
UnsupportedBackend — safe no-op PlatformBackend for any non-Windows sys.platform.

Exists only so that constructing xGen's objects (InspectMode, TransientCapturer,
OverlayWindow, MainWindow, ...) never crashes off Windows. It does not attempt
to provide a working native-inspection experience on another OS — that is
out of scope (see the project's Windows-Guard-Platform-Abstraction-Plan).
Every method returns an empty/None/False result plus, where useful, a short
`unavailable_reason` a caller can surface to the user.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from xgen.platform.backend import NativeElement, TransientSnapshotResult


class UnsupportedBackend:
    """No-op PlatformBackend used whenever sys.platform != "win32"."""

    unavailable_reason = "Live native inspection isn't available on this OS yet."

    # --- Accessibility ---

    def initialize_accessibility(self) -> None:
        pass

    def is_accessibility_available(self) -> bool:
        return False

    def element_from_point(self, x: int, y: int) -> Optional[NativeElement]:
        return None

    def walk_subtree(
        self,
        root_element: Optional[NativeElement] = None,
        max_depth: int = 20,
        max_elements: int = 250,
    ) -> List[NativeElement]:
        return []

    def capture_transient_snapshot(self, cursor_x: int, cursor_y: int) -> TransientSnapshotResult:
        return TransientSnapshotResult(status="unavailable", message=self.unavailable_reason)

    # --- Window enumeration / hit-testing ---

    def get_open_windows(self) -> List:
        from xgen.utils.window_finder import _DESKTOP_ROOT
        return [_DESKTOP_ROOT]

    def window_from_point(self, x: int, y: int) -> Optional[int]:
        return None

    def get_process_id_for_window(self, hwnd: int) -> Optional[int]:
        return None

    def native_window_id_for_widget(self, widget: object) -> Optional[int]:
        return None

    # --- Overlay native styling ---

    def apply_click_through(self, widget: object) -> None:
        pass

    def enforce_topmost(self, widget: object) -> None:
        pass

    # --- Display / DPI ---

    def init_dpi_awareness(self) -> None:
        pass

    def get_physical_cursor_pos(self) -> Tuple[int, int]:
        # Same Qt-based fallback WindowsBackend uses when the native call fails.
        from xgen.utils.dpi import get_screen_dpr_at
        from PyQt6.QtGui import QCursor
        pos = QCursor.pos()
        dpr = get_screen_dpr_at(pos.x(), pos.y())
        return int(pos.x() * dpr), int(pos.y() * dpr)

    def get_dpi_for_window(self, hwnd: int) -> int:
        return 96

    # --- Privilege ---

    def is_running_as_admin(self) -> bool:
        import os
        return os.getuid() == 0 if hasattr(os, "getuid") else False

    def relaunch_as_admin(self) -> bool:
        return False

    # --- Cursor / app identity ---

    def move_cursor_to(self, x: int, y: int) -> bool:
        return False

    def set_app_user_model_id(self, app_id: str) -> None:
        pass

    # --- Input suppression ---

    def mouse_filter_kwarg_name(self) -> Optional[str]:
        return None

    def decode_mouse_button_event(self, event_type: object, native_event: object) -> Optional[Tuple[int, int, bool]]:
        return None

    def default_driver_platform(self) -> str:
        # No native backend here, but a user on Linux can still point xGen at
        # a remote Appium server; Windows stays the historical default.
        return "windows"

    def uses_physical_pixel_coords(self) -> bool:
        return True
