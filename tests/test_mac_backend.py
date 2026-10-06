"""
Tests for MacBackend and the factory's macOS branch.

These run on any platform (including this project's Linux CI sandbox and the
user's Windows dev machine) — they exercise the *guarded-import* and
*graceful-degradation* behavior, not real AXUIElement/Quartz calls, since
PyObjC is only installable on macOS. Real behavior (element_from_point,
window enumeration, overlay click-through, etc.) can only be verified live
on an actual Mac — see claude/macOS-Platform-Backend-Plan.md for the list of
what still needs that live verification.
"""

from __future__ import annotations

import sys

import pytest

from xgen.platform.factory import get_platform_backend, reset_platform_backend_for_tests
from xgen.platform.mac_backend import MacBackend


@pytest.fixture(autouse=True)
def _reset_backend_singleton():
    reset_platform_backend_for_tests()
    yield
    reset_platform_backend_for_tests()


def test_factory_returns_mac_backend_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    backend = get_platform_backend()
    assert isinstance(backend, MacBackend)


## NOTE on what a "Phase 0, import under darwin" guard-rail test would need:
## monkeypatching sys.platform to "darwin" in a non-Mac sandbox also fools
## pynput's OWN internal platform detection (xgen.ui.main_window transitively
## imports pynput via capture/mouse_hook.py and capture/keyboard_hook.py), so
## it tries to load pynput's real darwin backend — which needs the actual
## Quartz package, unavailable on Linux/Windows. That failure is pynput's
## genuine inability to run outside macOS, not a bug in xGen's own platform
## guards (which this file's other tests already cover directly). The
## meaningful equivalent of that guard rail is the macOS CI job added in
## .github/workflows/build-and-release.yml, which imports and runs the full
## test suite on a real macos-latest runner with the real frameworks present.


def test_mac_backend_is_inert_when_pyobjc_is_unavailable(monkeypatch):
    """
    Simulates a Mac with the native frameworks missing (e.g. a broken PyObjC
    install) by forcing every HAS_* flag off, mirroring how
    test_platform_factory.py's test_unsupported_backend_is_inert covers
    UnsupportedBackend. Every method must degrade to a safe default instead
    of raising.
    """
    import xgen.platform.mac_backend as mb

    monkeypatch.setattr(mb, "HAS_AX", False)
    monkeypatch.setattr(mb, "HAS_OBJC", False)
    monkeypatch.setattr(mb, "HAS_APPLICATIONSERVICES", False)
    monkeypatch.setattr(mb, "HAS_QUARTZ", False)
    monkeypatch.setattr(mb, "HAS_APPKIT", False)

    backend = mb.MacBackend()
    assert backend.is_accessibility_available() is False
    assert backend.element_from_point(0, 0) is None
    assert backend.walk_subtree() == []
    snapshot = backend.capture_transient_snapshot(0, 0)
    assert snapshot.status == "unavailable"

    # Unlike WindowsBackend, this one never synthesizes a Desktop Root entry:
    # Appium's Mac2 driver attaches to a single application, so a desktop-wide
    # target would promise something no Mac session can deliver (and picking it
    # silently lands the user on Finder, Mac2Driver's own default).
    assert backend.get_open_windows() == []
    assert backend.window_from_point(0, 0) is None
    assert backend.get_process_id_for_window(1) is None
    assert backend.native_window_id_for_widget(object()) is None

    # No-op styling/lifecycle calls must not raise on a bare object.
    backend.apply_click_through(object())
    backend.enforce_topmost(object())
    backend.init_dpi_awareness()
    backend.set_app_user_model_id("anything")

    assert backend.get_dpi_for_window(1) == 96
    assert backend.is_running_as_admin() is False
    assert backend.move_cursor_to(0, 0) is False
    assert backend.mouse_filter_kwarg_name() is None
    assert backend.decode_mouse_button_event(None, None) is None

    # get_physical_cursor_pos() falls back to the Qt-based path (needs a QApplication).
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    x, y = backend.get_physical_cursor_pos()
    assert isinstance(x, int) and isinstance(y, int)


def test_relaunch_as_admin_never_exits_process(monkeypatch):
    """
    relaunch_as_admin() must return False (never claim it initiated a
    relaunch) since there is no macOS equivalent of Windows' UAC relaunch —
    see mac_backend.py's Privilege section docstring.
    """
    import xgen.platform.mac_backend as mb
    monkeypatch.setattr(mb.subprocess, "Popen", lambda *a, **k: None)
    backend = mb.MacBackend()
    assert backend.relaunch_as_admin() is False
