"""
Global Mouse Hook using pynput.
Intercepts left-click coordinates during Inspect Mode and suppresses clicks on target applications
so inspected controls are selected in xGen without triggering their native action in the target app.
"""

from __future__ import annotations

import logging
import platform
from typing import Callable, Optional
from PyQt6.QtCore import QObject, pyqtSignal
from pynput import mouse

logger = logging.getLogger("xgen.mouse_hook")

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2


class MouseHook(QObject):
    """
    Global low-level mouse hook emitting signals on left-click and intercepting clicks on target apps.
    """
    left_clicked = pyqtSignal(int, int)   # screen_x, screen_y

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._listener: Optional[mouse.Listener] = None
        self._filter: Optional[Callable[[int, int], bool]] = None
        self._is_active = False
        # True whenever a native suppression callback (win32_event_filter or a
        # backend's darwin_intercept-equivalent) is doing the real work this
        # session, so _on_click (pynput's *separate*, always-additionally-fired
        # plain callback) knows to stay out of the way instead of double-emitting.
        self._native_filter_active = False

    def start(self, callback_filter: Optional[Callable[[int, int], bool]] = None) -> None:
        """
        Start the background mouse listener thread with event suppression on target apps.
        callback_filter: Return True if outside xGen (intercept click), False if inside xGen (allow click).
        """
        self.stop()
        self._filter = callback_filter
        self._is_active = True

        if platform.system() == "Windows":
            self._native_filter_active = True
            self._listener = mouse.Listener(
                on_click=self._on_click,
                win32_event_filter=self._win32_event_filter
            )
        else:
            # Ask the current PlatformBackend whether it exposes a pynput
            # suppression kwarg (macOS's "darwin_intercept" today) instead of
            # checking platform.system() == "Darwin" here directly — this file
            # stays free of any platform-specific import (Quartz, etc.); all
            # native event decoding lives behind xgen.platform.factory.
            from xgen.platform.factory import get_platform_backend
            kwarg_name = get_platform_backend().mouse_filter_kwarg_name()
            if kwarg_name:
                self._native_filter_active = True
                self._listener = mouse.Listener(on_click=self._on_click, **{kwarg_name: self._platform_intercept})
            else:
                self._native_filter_active = False
                self._listener = mouse.Listener(on_click=self._on_click)

        self._listener.daemon = True
        self._listener.start()
        logger.debug("Global mouse hook started with click interception.")

    def stop(self) -> None:
        """Stop and terminate background mouse listener."""
        self._is_active = False
        self._native_filter_active = False
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as e:
                logger.debug("Mouse listener stop note: %s", e)
            self._listener = None
            logger.debug("Global mouse hook stopped.")

    def _win32_event_filter(self, msg: int, data: object) -> bool:
        """
        Windows low-level hook filter.
        Calls suppress_event() to completely block clicks from reaching the target application.
        """
        if not self._is_active:
            return True

        if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP):
            px = int(data.pt.x)
            py = int(data.pt.y)

            # If filter returns False (click is inside xGen window), allow click through!
            if self._filter is not None and not self._filter(px, py):
                return True

            # Click is on target app: emit selection on button down
            if msg in (WM_LBUTTONDOWN, WM_NCLBUTTONDOWN):
                self.left_clicked.emit(px, py)

            # Suppress click event from reaching target application in Windows
            if self._listener is not None:
                self._listener.suppress_event()

            return False

        return True

    def _platform_intercept(self, event_type: object, native_event: object) -> object:
        """
        Generic native-suppression callback for any PlatformBackend that exposes
        mouse_filter_kwarg_name() (macOS's darwin_intercept today). All event
        decoding is delegated to the backend (see xgen/platform/mac_backend.py)
        so this file never imports a platform-specific package itself.

        NOTE: the pass/suppress contract here (return the original event to let
        it through, return None to suppress) matches pynput's darwin_intercept
        semantics specifically. If a future third backend used this same kwarg
        name with different return-value semantics, this method would need to
        ask the backend for that too rather than assuming it.
        """
        if not self._is_active:
            return native_event

        from xgen.platform.factory import get_platform_backend
        decoded = get_platform_backend().decode_mouse_button_event(event_type, native_event)
        if decoded is None:
            return native_event  # Not an event xGen cares about: pass through unmodified.

        px, py, is_press = decoded

        # If filter returns False (click is inside xGen window), allow click through!
        if self._filter is not None and not self._filter(px, py):
            return native_event

        if is_press:
            self.left_clicked.emit(px, py)

        return None  # Suppress: block the click from reaching the app underneath.

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        """
        Fallback callback for platforms/backends with no native suppression
        kwarg wired up (e.g. Linux, or a Mac without the Quartz/PyObjC
        frameworks available). pynput always fires this callback *in addition
        to* any win32_event_filter/darwin_intercept callback, so whenever one
        of those is active, this one must stay out of the way entirely —
        otherwise every click would be emitted twice.
        """
        if self._native_filter_active:
            return None

        if not self._is_active or not pressed or button != mouse.Button.left:
            return None

        if self._filter is not None and not self._filter(x, y):
            return None

        self.left_clicked.emit(int(x), int(y))
        return None
