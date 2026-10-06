"""
Tests for the hover-highlight blink class of bug.

The highlight overlay is drawn directly under the cursor and sits at the top
of the window stack. Every hit-test xGen performs while Inspect Mode is active
therefore lands on it unless something deliberately excludes it, and when one
does, the symptom is always the same: hover resolution is suppressed, the
overlay hides, the next poll re-resolves it, and the box blinks at the poll
rate.

These cover the four places that has to be handled:
  * MacBackend.window_from_point   -- skips our own click-through windows
  * MacBackend.element_from_point  -- re-enters a foreign app when the
                                      accessibility hit-test returns our own
  * WindowsBackend.window_from_point -- same skip, via WS_EX_TRANSPARENT HWNDs
  * InspectMode._poll_hover        -- hysteresis, so one empty resolution
                                      cannot produce a visible flicker
"""

from __future__ import annotations

import os

import pytest

from xgen.platform.mac_backend import MacBackend
from xgen.utils.rect import Rect


# ----------------------------------------------------------------------
# MacBackend hit-testing
# ----------------------------------------------------------------------

class _FakeNSWindow:
    def __init__(self, number: int):
        self._number = number
        self.ignores_mouse = False

    def setIgnoresMouseEvents_(self, value):  # noqa: N802 - ObjC selector name
        self.ignores_mouse = value

    def windowNumber(self):  # noqa: N802 - ObjC selector name
        return self._number


def _window_info(number: int, pid: int, x=0, y=0, w=100, h=100):
    return {
        "kCGWindowNumber": number,
        "kCGWindowOwnerPID": pid,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
    }


class _FakeQuartz:
    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 2
    kCGNullWindowID = 0

    def __init__(self, windows):
        self._windows = windows

    def CGWindowListCopyWindowInfo(self, options, relative):  # noqa: N802
        return self._windows


def _patch_quartz(monkeypatch, windows):
    import xgen.platform.mac_backend as mb
    monkeypatch.setattr(mb, "HAS_QUARTZ", True)
    monkeypatch.setattr(mb, "Quartz", _FakeQuartz(windows), raising=False)


def test_window_from_point_skips_our_own_click_through_overlay(monkeypatch):
    """
    The overlay is the frontmost window at the cursor. Without the skip,
    window_from_point reports it, the hover filter sees an xGen-owned window
    and suppresses hover — the blink.
    """
    overlay_id, app_id = 900, 42
    _patch_quartz(monkeypatch, [
        _window_info(overlay_id, os.getpid()),   # frontmost: our overlay
        _window_info(app_id, 1234),              # behind it: the real app
    ])

    backend = MacBackend()
    # Nothing registered yet: the overlay wins, which is the buggy behaviour.
    assert backend.window_from_point(10, 10) == overlay_id

    backend._click_through_ids[id(object)] = overlay_id
    assert backend.window_from_point(10, 10) == app_id


def test_apply_click_through_records_the_window_number(monkeypatch):
    """The id has to be re-recorded on every call: Qt can recreate the native
    NSWindow across a hide/show cycle, and a stale id is what un-fixes this."""
    import xgen.platform.mac_backend as mb
    monkeypatch.setattr(mb, "HAS_OBJC", True)

    widget = object()
    first, second = _FakeNSWindow(101), _FakeNSWindow(202)

    backend = MacBackend()
    monkeypatch.setattr(MacBackend, "_ns_window_for_widget", staticmethod(lambda w: first))
    backend.apply_click_through(widget)
    assert first.ignores_mouse is True
    assert backend._click_through_window_ids() == {101}

    monkeypatch.setattr(MacBackend, "_ns_window_for_widget", staticmethod(lambda w: second))
    backend.apply_click_through(widget)
    assert backend._click_through_window_ids() == {202}, "stale id must not linger"


def test_element_from_point_reenters_foreign_app_when_hit_is_our_own(monkeypatch):
    """
    setIgnoresMouseEvents_ governs event routing, not the accessibility
    hit-test, so AXUIElementCopyElementAtPosition still returns the overlay.
    When it does, resolution must continue into the topmost window at that
    point belonging to another process rather than returning our own chrome.
    """
    import xgen.platform.mac_backend as mb

    own_element = "OVERLAY_AX_ELEMENT"
    foreign_app_element = "TEXTEDIT_AX_APP"
    created_for_pids = []

    class _FakeAXFW:
        @staticmethod
        def AXUIElementCreateSystemWide():  # noqa: N802
            return "SYSTEM_WIDE"

        @staticmethod
        def AXUIElementCopyElementAtPosition(sysw, x, y, _):  # noqa: N802
            return (0, own_element)

        @staticmethod
        def AXUIElementGetPid(element, _):  # noqa: N802
            return (0, os.getpid()) if element is own_element else (0, 1234)

        @staticmethod
        def AXUIElementCreateApplication(pid):  # noqa: N802
            created_for_pids.append(pid)
            return foreign_app_element

    monkeypatch.setattr(mb, "HAS_AX", True)
    monkeypatch.setattr(mb, "AXFW", _FakeAXFW, raising=False)
    _patch_quartz(monkeypatch, [
        _window_info(900, os.getpid()),   # our overlay, frontmost
        _window_info(901, 1234),          # TextEdit behind it
    ])

    drilled_from = []
    monkeypatch.setattr(
        MacBackend, "_drill_down",
        classmethod(lambda cls, el, x, y: drilled_from.append(el) or el),
    )
    monkeypatch.setattr(
        MacBackend, "_element_to_native",
        classmethod(lambda cls, el: el),
    )

    backend = MacBackend()
    result = backend.element_from_point(10, 10)

    assert created_for_pids == [1234], "should have re-entered via the foreign app's pid"
    assert drilled_from == [foreign_app_element]
    assert result is foreign_app_element


def test_element_from_point_keeps_foreign_hit_untouched(monkeypatch):
    """The common case — the hit is already another app — must not take the
    slower re-entry path."""
    import xgen.platform.mac_backend as mb

    foreign = "SOME_BUTTON"

    class _FakeAXFW:
        @staticmethod
        def AXUIElementCreateSystemWide():  # noqa: N802
            return "SYSTEM_WIDE"

        @staticmethod
        def AXUIElementCopyElementAtPosition(sysw, x, y, _):  # noqa: N802
            return (0, foreign)

        @staticmethod
        def AXUIElementGetPid(element, _):  # noqa: N802
            return (0, 1234)

        @staticmethod
        def AXUIElementCreateApplication(pid):  # noqa: N802
            raise AssertionError("must not re-enter for a foreign hit")

    monkeypatch.setattr(mb, "HAS_AX", True)
    monkeypatch.setattr(mb, "AXFW", _FakeAXFW, raising=False)
    monkeypatch.setattr(MacBackend, "_drill_down", classmethod(lambda cls, el, x, y: el))
    monkeypatch.setattr(MacBackend, "_element_to_native", classmethod(lambda cls, el: el))

    assert MacBackend().element_from_point(10, 10) is foreign


def test_element_from_point_returns_none_when_only_our_windows_are_there(monkeypatch):
    """Cursor over xGen with nothing behind it: no element, and no crash."""
    import xgen.platform.mac_backend as mb

    class _FakeAXFW:
        @staticmethod
        def AXUIElementCreateSystemWide():  # noqa: N802
            return "SYSTEM_WIDE"

        @staticmethod
        def AXUIElementCopyElementAtPosition(sysw, x, y, _):  # noqa: N802
            return (0, "OURS")

        @staticmethod
        def AXUIElementGetPid(element, _):  # noqa: N802
            return (0, os.getpid())

        @staticmethod
        def AXUIElementCreateApplication(pid):  # noqa: N802
            raise AssertionError("no foreign window exists to re-enter")

    monkeypatch.setattr(mb, "HAS_AX", True)
    monkeypatch.setattr(mb, "AXFW", _FakeAXFW, raising=False)
    _patch_quartz(monkeypatch, [_window_info(900, os.getpid())])

    assert MacBackend().element_from_point(10, 10) is None


# ----------------------------------------------------------------------
# WindowsBackend parity
# ----------------------------------------------------------------------

def test_windows_window_from_point_skips_click_through_hwnd(monkeypatch):
    """Same contract on Windows: an overlay HWND is never reported."""
    import xgen.platform.windows_backend as wb

    monkeypatch.setattr(wb, "HAS_WIN32_WINDOW_APIS", True)

    class _FakeWin32Gui:
        @staticmethod
        def WindowFromPoint(pt):  # noqa: N802
            return 5150

    monkeypatch.setattr(wb, "win32gui", _FakeWin32Gui, raising=False)

    backend = wb.WindowsBackend()
    assert backend.window_from_point(10, 10) == 5150

    backend._click_through_ids = {id(object): 5150}
    assert backend.window_from_point(10, 10) is None


# ----------------------------------------------------------------------
# Hover hysteresis
# ----------------------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.style_mode = "hover"
        self.hidden_calls = 0
        self.last_rect = None

    def hide_overlay(self):
        self.hidden_calls += 1

    def highlight_hover(self, rect):
        self.last_rect = rect


def _inspect_mode(monkeypatch, overlay, element_result):
    # InspectMode pulls in MouseHook, which imports pynput at module scope.
    # On a headless Linux box pynput raises ImportError from inside its own
    # package (no X display) rather than ModuleNotFoundError, which
    # pytest.importorskip no longer treats as skippable — so catch it here.
    # Both CI runners (windows-latest, macos-latest) have a working pynput.
    try:
        import pynput  # noqa: F401
    except ImportError as e:
        pytest.skip(f"MouseHook needs pynput, unavailable here: {e}")

    from xgen.capture.inspect_mode import InspectMode
    from xgen.config import XGenConfig
    import xgen.capture.inspect_mode as im

    class _FakeBackend:
        def element_from_point(self, x, y):
            return element_result()

    monkeypatch.setattr(im, "get_platform_backend", lambda: _FakeBackend())
    monkeypatch.setattr(im, "get_physical_cursor_pos", lambda: (10, 10))

    mode = InspectMode(XGenConfig(), overlay=overlay)
    mode._is_active = True
    return mode


def test_one_empty_poll_does_not_hide_the_highlight(qapp, monkeypatch):
    """A single unlucky hit-test must not produce a visible flicker."""
    overlay = _FakeOverlay()
    mode = _inspect_mode(monkeypatch, overlay, lambda: None)

    mode._poll_hover()
    assert overlay.hidden_calls == 0


def test_sustained_empty_polls_do_hide_the_highlight(qapp, monkeypatch):
    """Hysteresis must not turn into a highlight that never goes away."""
    overlay = _FakeOverlay()
    mode = _inspect_mode(monkeypatch, overlay, lambda: None)

    for _ in range(mode.EMPTY_TICKS_BEFORE_HIDE):
        mode._poll_hover()
    assert overlay.hidden_calls >= 1


def test_a_resolved_element_resets_the_empty_streak(qapp, monkeypatch):
    """Alternating empty/resolved polls — exactly what the blink looked like —
    must never reach the hide threshold."""
    from xgen.platform.backend import NativeElement

    overlay = _FakeOverlay()
    results = [None, NativeElement(runtime_id="", control_type="X", name="", automation_id="",
                                   bounding_rect=Rect(0, 0, 10, 10))]
    idx = {"i": 0}

    def _next():
        r = results[idx["i"] % len(results)]
        idx["i"] += 1
        return r

    mode = _inspect_mode(monkeypatch, overlay, _next)
    for _ in range(10):
        mode._poll_hover()

    assert overlay.hidden_calls == 0
