"""
Three macOS behaviours reported from live use, pinned here so they can't
regress:

  1. Disconnecting a session closed the target application. Mac2Driver
     terminates the app it is attached to on DELETE /session unless told not
     to, so quitting xGen quit the user's TextEdit with it.
  2. The //Window prefix toggle did nothing on a Mac session, because a Mac2
     session is already scoped to one application and its root is
     XCUIElementTypeApplication, not a window.
  3. Full-screen highlight suppression stopped working on Retina displays
     after native coordinates on macOS became points rather than pixels.
"""

from __future__ import annotations

import json

import pytest
import responses

from xgen.config import XGenConfig
from xgen.core.driver_dialect import MAC2_DIALECT, WINDOWS_DIALECT, DriverDialectStore
from xgen.core.session_manager import SessionManager
from xgen.utils.rect import Rect

BASE = "http://127.0.0.1:4723"


@pytest.fixture(autouse=True)
def _reset_active_dialect():
    DriverDialectStore.instance().reset()
    yield
    DriverDialectStore.instance().reset()


def _sent_capabilities() -> dict:
    for call in responses.calls:
        if call.request.method == "POST" and call.request.url.rstrip("/").endswith("/session"):
            return json.loads(call.request.body)["capabilities"]["alwaysMatch"]
    raise AssertionError("No POST /session request was made")


def _mac_session(cfg_overrides: dict) -> dict:
    responses.add(
        responses.GET, f"{BASE}/status",
        json={"value": {"ready": True}}, status=200,
    )
    responses.add(
        responses.GET, f"{BASE}/sessions",
        json={"value": []}, status=200,
    )
    responses.add(
        responses.POST, f"{BASE}/session",
        json={"value": {"sessionId": "mac-1", "capabilities": {"platformName": "mac"}}},
        status=200,
    )
    cfg = XGenConfig()
    cfg.appium_url = BASE
    cfg.target_platform = "mac"
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)

    mgr = SessionManager()
    try:
        mgr._create_session(cfg)
        return _sent_capabilities()
    finally:
        # A live SessionManager leaves a heartbeat timer and worker thread
        # running; left behind, they touch a half-torn-down interpreter during
        # a later garbage collection and abort the whole test process.
        mgr.close()


# ----------------------------------------------------------------------
# 1. Disconnecting must not close the user's app
# ----------------------------------------------------------------------

@responses.activate
def test_mac_session_never_kills_the_app_it_attached_to(qapp):
    """The regression: attaching from the app picker left skipAppKill unset,
    so disconnecting quit the app out from under the user."""
    caps = _mac_session({"app_bundle_id": "com.apple.TextEdit", "attach_to_running": False})
    assert caps["appium:skipAppKill"] is True


@responses.activate
def test_mac_skip_app_kill_applies_to_launched_apps_too(qapp):
    """xGen is an inspector; it must not destroy what it inspects, however the
    session was started."""
    caps = _mac_session({"app_path": "/Applications/TextEdit.app", "app_bundle_id": ""})
    assert caps["appium:skipAppKill"] is True


@responses.activate
def test_attach_to_running_still_sets_no_reset(qapp):
    """skipAppKill moving out of the attach branch must not take noReset with it."""
    caps = _mac_session({"app_bundle_id": "com.apple.TextEdit", "attach_to_running": True})
    assert caps["appium:noReset"] is True


@responses.activate
def test_no_reset_stays_off_when_not_attaching(qapp):
    caps = _mac_session({"app_bundle_id": "com.apple.TextEdit", "attach_to_running": False})
    assert "appium:noReset" not in caps


@responses.activate
def test_a_user_capability_can_still_override_skip_app_kill(qapp):
    """Extra capabilities are applied last, so the escape hatch stays open."""
    caps = _mac_session({
        "app_bundle_id": "com.apple.TextEdit",
        "extra_capabilities": json.dumps({"appium:skipAppKill": False}),
    })
    assert caps["appium:skipAppKill"] is False


# ----------------------------------------------------------------------
# 2. The //Window prefix toggle
# ----------------------------------------------------------------------

def test_window_prefix_is_offered_on_windows_and_not_on_mac2():
    assert WINDOWS_DIALECT.supports_window_prefix is True
    assert MAC2_DIALECT.supports_window_prefix is False


def test_prefix_toggle_hides_itself_for_a_mac_session(qapp):
    from xgen.ui.xpath_panel import XPathPanel

    panel = XPathPanel()
    DriverDialectStore.instance().set_active(WINDOWS_DIALECT)
    panel.apply_driver_dialect()
    assert panel.chk_prefix.isVisible() or not panel.isVisible()  # visible unless parent is hidden
    assert not panel.chk_prefix.isHidden()

    DriverDialectStore.instance().set_active(MAC2_DIALECT)
    panel.apply_driver_dialect()
    assert panel.chk_prefix.isHidden()


def test_hiding_the_toggle_also_clears_it(qapp):
    """A hidden-but-checked box would keep padding every selector with a
    prefix the user can no longer see or turn off."""
    from xgen.ui.xpath_panel import XPathPanel

    panel = XPathPanel()
    DriverDialectStore.instance().set_active(WINDOWS_DIALECT)
    panel.apply_driver_dialect()
    panel.chk_prefix.setChecked(True)

    DriverDialectStore.instance().set_active(MAC2_DIALECT)
    panel.apply_driver_dialect()
    assert panel.chk_prefix.isChecked() is False


# ----------------------------------------------------------------------
# 3. Full-screen suppression across both coordinate conventions
# ----------------------------------------------------------------------

def _overlay(qapp):
    from xgen.capture.overlay_window import OverlayWindow
    return OverlayWindow()


def _screen_geometry(qapp):
    return qapp.primaryScreen().geometry()


def test_fullscreen_rect_is_suppressed_in_point_space(qapp, monkeypatch):
    """macOS: native coordinates are points, so a desktop-sized rect has the
    screen's *logical* dimensions. Scaling the screen by devicePixelRatio
    here is what made this test silently never fire on a Retina Mac."""
    import xgen.capture.overlay_window as ow
    monkeypatch.setattr(ow, "get_screen_dpr_at", lambda x, y: 1.0)

    overlay = _overlay(qapp)
    g = _screen_geometry(qapp)
    full = Rect(g.left(), g.top(), g.left() + g.width(), g.top() + g.height())
    assert overlay._is_fullscreen_rect(full) is True


def test_fullscreen_rect_is_suppressed_in_physical_pixel_space(qapp, monkeypatch):
    """Windows: native coordinates are physical pixels, so the same desktop
    rect is devicePixelRatio times larger. Both must be recognised."""
    import xgen.capture.overlay_window as ow
    monkeypatch.setattr(ow, "get_screen_dpr_at", lambda x, y: 2.0)

    overlay = _overlay(qapp)
    g = _screen_geometry(qapp)
    full = Rect(g.left() * 2, g.top() * 2,
                g.left() * 2 + g.width() * 2, g.top() * 2 + g.height() * 2)
    assert overlay._is_fullscreen_rect(full) is True


def test_an_ordinary_element_rect_is_not_suppressed(qapp, monkeypatch):
    import xgen.capture.overlay_window as ow
    monkeypatch.setattr(ow, "get_screen_dpr_at", lambda x, y: 1.0)

    overlay = _overlay(qapp)
    assert overlay._is_fullscreen_rect(Rect(100, 100, 300, 160)) is False
