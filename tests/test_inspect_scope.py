"""
Inspect Mode scoping to the attached application.

element_from_point is a *system-wide* hit test: it answers "what is under the
cursor", not "what is under the cursor inside the app this session is attached
to". On Windows that is right — a Desktop Root session genuinely targets the
whole desktop. An Appium Mac2 session covers exactly one application, so an
element in any other app can never be in the fetched tree and no XPath from
that session can address it. Highlighting one anyway invites the user to click
something xGen cannot then generate a selector for, which is what made the
packaged macOS build look broken.
"""

from __future__ import annotations

import pytest

from xgen.core.driver_dialect import MAC2_DIALECT, WINDOWS_DIALECT, DriverDialectStore
from xgen.platform.backend import NativeElement
from xgen.utils.rect import Rect


@pytest.fixture(autouse=True)
def _reset_dialect():
    yield
    DriverDialectStore.instance().reset()


class _FakeOverlay:
    def __init__(self):
        self.style_mode = "hover"
        self.hidden_calls = 0
        self.highlights = []

    def hide_overlay(self):
        self.hidden_calls += 1

    def highlight_hover(self, rect):
        self.highlights.append(rect)


def _element():
    return NativeElement(
        runtime_id="", control_type="XCUIElementTypeButton", name="Save",
        automation_id="", bounding_rect=Rect(0, 0, 40, 20),
    )


def _mode(monkeypatch, overlay, cursor=(10, 10)):
    try:
        import pynput  # noqa: F401
    except ImportError as e:
        pytest.skip(f"MouseHook needs pynput, unavailable here: {e}")

    from xgen.capture.inspect_mode import InspectMode
    from xgen.config import XGenConfig
    import xgen.capture.inspect_mode as im

    class _FakeBackend:
        def element_from_point(self, x, y):
            return _element()

    monkeypatch.setattr(im, "get_platform_backend", lambda: _FakeBackend())
    monkeypatch.setattr(im, "get_physical_cursor_pos", lambda: cursor)

    mode = InspectMode(XGenConfig(), overlay=overlay)
    mode._is_active = True
    return mode


# ----------------------------------------------------------------------
# InspectMode
# ----------------------------------------------------------------------

def test_hovering_inside_the_attached_app_resolves_normally(qapp, monkeypatch):
    overlay = _FakeOverlay()
    mode = _mode(monkeypatch, overlay)
    mode.set_scope_filter(lambda x, y: True)

    mode._poll_hover()
    assert overlay.highlights, "an element inside the attached app must still highlight"


def test_hovering_outside_the_attached_app_resolves_nothing(qapp, monkeypatch):
    overlay = _FakeOverlay()
    mode = _mode(monkeypatch, overlay)
    mode.set_scope_filter(lambda x, y: False)

    for _ in range(mode.EMPTY_TICKS_BEFORE_HIDE):
        mode._poll_hover()

    assert overlay.highlights == [], "nothing outside the attached app may highlight"
    assert overlay.hidden_calls >= 1


def test_no_scope_filter_means_inspect_everything(qapp, monkeypatch):
    """A Windows Desktop Root session targets the whole desktop; scoping it
    would break the one mode whose entire purpose is to span applications."""
    overlay = _FakeOverlay()
    mode = _mode(monkeypatch, overlay)
    mode.set_scope_filter(None)

    mode._poll_hover()
    assert overlay.highlights


def test_scope_transitions_are_announced_once_not_every_poll(qapp, monkeypatch):
    overlay = _FakeOverlay()
    mode = _mode(monkeypatch, overlay)

    seen = []
    mode.scope_changed.connect(seen.append)

    inside = {"v": True}
    mode.set_scope_filter(lambda x, y: inside["v"])

    for _ in range(3):
        mode._poll_hover()
    inside["v"] = False
    for _ in range(3):
        mode._poll_hover()
    inside["v"] = True
    for _ in range(3):
        mode._poll_hover()

    assert seen == [False, True], f"one signal per transition, got {seen}"


def test_a_click_outside_the_attached_app_is_ignored(qapp, monkeypatch):
    """The highlight is suppressed, so a click out there would otherwise fire
    on a stale element from the last in-scope hover."""
    overlay = _FakeOverlay()
    mode = _mode(monkeypatch, overlay)
    mode.set_scope_filter(lambda x, y: True)
    mode._poll_hover()           # prime _last_uia_el while in scope
    assert mode._last_uia_el is not None

    clicked = []
    mode.element_clicked.connect(lambda el, x, y: clicked.append(el))

    mode.set_scope_filter(lambda x, y: False)
    mode._on_mouse_click(500, 500)
    assert clicked == []


# ----------------------------------------------------------------------
# Backend support
# ----------------------------------------------------------------------

def test_mac_process_id_at_point_skips_our_own_click_through_overlay(monkeypatch):
    from tests.test_overlay_hit_test import _patch_quartz, _window_info
    from xgen.platform.mac_backend import MacBackend

    overlay_id = 900
    _patch_quartz(monkeypatch, [
        _window_info(overlay_id, 4321),   # our overlay, frontmost
        _window_info(901, 1234),          # the app underneath
    ])
    backend = MacBackend()
    backend._click_through_ids[id(object)] = overlay_id

    assert backend.process_id_at_point(10, 10) == 1234


def test_mac_enumeration_records_the_owning_pid(monkeypatch):
    from tests.test_overlay_hit_test import _patch_quartz, _window_info
    from xgen.platform.mac_backend import MacBackend

    _patch_quartz(monkeypatch, [_window_info(901, 1234)])
    monkeypatch.setattr(MacBackend, "_bundle_id_for_pid", staticmethod(lambda pid: "com.apple.TextEdit"))

    targets = MacBackend().get_open_windows()
    assert targets and targets[0].pid == 1234, "the picker must carry the pid the scope filter needs"


# ----------------------------------------------------------------------
# Which sessions get scoped
# ----------------------------------------------------------------------

def test_a_mac2_session_is_scoped_and_a_windows_one_is_not(qapp, monkeypatch):
    from xgen.config import XGenConfig
    from xgen.ui.main_window import MainWindow
    from xgen.utils.window_finder import WindowTarget

    win = MainWindow(XGenConfig(auto_connect_on_startup=False), start_hooks=False)
    try:
        win._last_window_targets = [
            WindowTarget(title="TextEdit", handle_hex="0x1", hwnd=1,
                         exe_name="com.apple.TextEdit",
                         bundle_id="com.apple.TextEdit", pid=4242),
        ]
        win.config.app_bundle_id = "com.apple.TextEdit"

        DriverDialectStore.instance().set_active(MAC2_DIALECT)
        win._apply_inspect_scope()
        assert win.inspect_mode._scope_filter is not None
        assert win._scope_pid == 4242

        DriverDialectStore.instance().set_active(WINDOWS_DIALECT)
        win._apply_inspect_scope()
        assert win.inspect_mode._scope_filter is None, "Desktop Root must keep spanning the desktop"
    finally:
        win.session_manager.close()
        win.tree_fetcher.close()
        win.close()


def test_an_unresolvable_target_leaves_inspect_mode_unscoped(qapp):
    """Fail open. An inspector that silently resolves nothing everywhere is far
    worse than one that occasionally resolves too much."""
    from xgen.config import XGenConfig
    from xgen.ui.main_window import MainWindow

    win = MainWindow(XGenConfig(auto_connect_on_startup=False), start_hooks=False)
    try:
        win._last_window_targets = []
        win.config.app_bundle_id = "com.apple.TextEdit"
        DriverDialectStore.instance().set_active(MAC2_DIALECT)

        win._apply_inspect_scope()
        assert win.inspect_mode._scope_filter is None
    finally:
        win.session_manager.close()
        win.tree_fetcher.close()
        win.close()
