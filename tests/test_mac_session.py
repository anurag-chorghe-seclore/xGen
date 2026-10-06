"""
Tests for starting a macOS (Appium Mac2 Driver) session, and for the
heartbeat change that keeps such a session alive.

Mac2Driver has no window-handles endpoint; before this, the 90-second
heartbeat would have read that "unknown command" reply as a dead session and
declared a perfectly healthy Mac session LOST. These tests pin both the
capabilities xGen sends and that failure mode.
"""

from __future__ import annotations

import json

import pytest
import responses
from PyQt6.QtCore import QCoreApplication

from xgen.config import XGenConfig
from xgen.core.driver_dialect import MAC2_DIALECT, WINDOWS_DIALECT, DriverDialectStore
from xgen.core.session_manager import SessionManager, SessionState

BASE = "http://127.0.0.1:4723"


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_active_dialect():
    DriverDialectStore.instance().reset()
    yield
    DriverDialectStore.instance().reset()


def _sent_capabilities() -> dict:
    """The alwaysMatch block from the POST /session request body."""
    for call in responses.calls:
        if call.request.method == "POST" and call.request.url.rstrip("/").endswith("/session"):
            body = json.loads(call.request.body)
            return body["capabilities"]["alwaysMatch"]
    raise AssertionError("No POST /session request was made")


def _sent_payload() -> dict:
    for call in responses.calls:
        if call.request.method == "POST" and call.request.url.rstrip("/").endswith("/session"):
            return json.loads(call.request.body)
    raise AssertionError("No POST /session request was made")


def _mock_mac_session(session_id: str = "mac-session-1") -> None:
    responses.add(responses.GET, f"{BASE}/sessions", json={"value": []}, status=200)
    responses.add(
        responses.POST,
        f"{BASE}/session",
        json={"value": {"sessionId": session_id,
                        "capabilities": {"platformName": "mac", "appium:automationName": "mac2"}}},
        status=200,
    )
    # Mac2Driver doesn't implement window handles.
    responses.add(
        responses.GET,
        f"{BASE}/session/{session_id}/window/handles",
        json={"value": {"error": "unknown command",
                        "message": "Method has not yet been implemented"}},
        status=404,
    )


# --- Capabilities -------------------------------------------------------------


@responses.activate
def test_mac_session_sends_mac2_capabilities_with_bundle_id(qapp):
    _mock_mac_session()
    mgr = SessionManager()
    try:
        cfg = XGenConfig(target_platform="mac", app_bundle_id="com.apple.TextEdit")
        info = mgr._create_session(cfg)

        caps = _sent_capabilities()
        assert caps["platformName"] == "mac"
        assert caps["appium:automationName"] == "mac2"
        assert caps["appium:bundleId"] == "com.apple.TextEdit"
        assert "appium:app" not in caps
        assert "appium:appTopLevelWindow" not in caps
        assert info.session_id == "mac-session-1"

        # Appium 2+ only driver: the deprecated JSONWP block is not sent.
        assert "desiredCapabilities" not in _sent_payload()
    finally:
        mgr.close()


@responses.activate
def test_mac_session_uses_app_path_when_given(qapp):
    _mock_mac_session()
    mgr = SessionManager()
    try:
        cfg = XGenConfig(target_platform="mac", app_path="/Applications/TextEdit.app")
        mgr._create_session(cfg)
        caps = _sent_capabilities()
        assert caps["appium:appPath"] == "/Applications/TextEdit.app"
        assert "appium:bundleId" not in caps
    finally:
        mgr.close()


@responses.activate
def test_mac_session_attach_without_relaunch_sets_no_reset(qapp):
    _mock_mac_session()
    mgr = SessionManager()
    try:
        cfg = XGenConfig(target_platform="mac", app_bundle_id="com.apple.Safari", attach_to_running=True)
        mgr._create_session(cfg)
        caps = _sent_capabilities()
        assert caps["appium:noReset"] is True
        assert caps["appium:skipAppKill"] is True
    finally:
        mgr.close()


@responses.activate
def test_mac_session_without_a_target_names_the_app_it_fell_back_to(qapp):
    """Mac2Driver silently defaults to Finder; xGen says so instead of showing a blank target."""
    _mock_mac_session()
    mgr = SessionManager()
    try:
        info = mgr._create_session(XGenConfig(target_platform="mac"))
        assert _sent_capabilities()["appium:bundleId"] == "com.apple.finder"
        assert "finder" in info.app_name.lower()
    finally:
        mgr.close()


@responses.activate
def test_windows_session_capabilities_are_unchanged(qapp):
    """Regression guard: the Windows payload must keep its exact previous shape."""
    responses.add(responses.GET, f"{BASE}/sessions", json={"value": []}, status=200)
    responses.add(
        responses.POST, f"{BASE}/session",
        json={"value": {"sessionId": "win-1", "capabilities": {}}}, status=200,
    )
    responses.add(responses.GET, f"{BASE}/session/win-1/window/handles", json={"value": []}, status=200)

    mgr = SessionManager()
    try:
        mgr._create_session(XGenConfig(target_platform="windows"))
        caps = _sent_capabilities()
        assert caps["platformName"] == "Windows"
        assert caps["appium:automationName"] == "Windows"
        assert caps["appium:app"] == "Root"
        # Legacy WinAppDriver compatibility block is still sent.
        payload = _sent_payload()
        assert payload["desiredCapabilities"]["app"] == "Root"
    finally:
        mgr.close()


# --- Dialect activation -------------------------------------------------------


def test_resolve_dialect_prefers_explicit_target_platform():
    assert SessionManager.resolve_dialect(XGenConfig(target_platform="mac")) is MAC2_DIALECT
    assert SessionManager.resolve_dialect(XGenConfig(target_platform="windows")) is WINDOWS_DIALECT


def test_resolve_dialect_falls_back_to_the_platform_backend(monkeypatch):
    """With no explicit choice, "what OS is this" is asked of the platform seam."""
    import xgen.platform.factory as factory

    class _FakeMacBackend:
        def default_driver_platform(self) -> str:
            return "mac"

    monkeypatch.setattr(factory, "get_platform_backend", lambda: _FakeMacBackend())
    assert SessionManager.resolve_dialect(XGenConfig()) is MAC2_DIALECT


@responses.activate
def test_server_reported_capabilities_override_the_assumed_dialect(qapp):
    """If the server negotiates a different driver, follow it rather than mis-parsing its XML."""
    responses.add(responses.GET, f"{BASE}/sessions", json={"value": []}, status=200)
    responses.add(
        responses.POST, f"{BASE}/session",
        json={"value": {"sessionId": "s1",
                        "capabilities": {"platformName": "mac", "appium:automationName": "mac2"}}},
        status=200,
    )
    responses.add(responses.GET, f"{BASE}/session/s1/window/handles", json={"value": []}, status=200)

    mgr = SessionManager()
    try:
        # Ask for Windows, but the server answers that it is a Mac2 session.
        mgr._create_session(XGenConfig(target_platform="windows"))
        assert DriverDialectStore.instance().active is MAC2_DIALECT
    finally:
        mgr.close()


# --- Heartbeat ----------------------------------------------------------------


@responses.activate
def test_heartbeat_treats_unknown_command_as_unsupported_not_a_dead_session(qapp):
    """
    A 404 "unknown command" means this driver lacks the endpoint; a 404
    "invalid session id" means the session is gone. Confusing the two is what
    would have killed every Mac session after 90 seconds.
    """
    mgr = SessionManager()
    try:
        mgr._session_id = "mac-session-1"
        mgr.state = SessionState.CONNECTED

        responses.add(
            responses.GET,
            f"{BASE}/session/mac-session-1/window/handles",
            json={"value": {"error": "unknown command", "message": "Method has not yet been implemented"}},
            status=404,
        )
        responses.add(
            responses.GET,
            f"{BASE}/session/mac-session-1/timeouts",
            json={"value": {"implicit": 0}},
            status=200,
        )

        assert mgr._heartbeat_probe_index == 0
        mgr._check_heartbeat()
        _join_heartbeat_threads()

        # Moved on to the next probe instead of declaring the session lost.
        assert mgr.state == SessionState.CONNECTED
        assert mgr._heartbeat_probe_index == 1

        mgr._check_heartbeat()
        _join_heartbeat_threads()
        assert mgr.state == SessionState.CONNECTED
        assert mgr._heartbeat_failures == 0
    finally:
        mgr.close()


@responses.activate
def test_heartbeat_still_reports_a_genuinely_dead_session(qapp):
    mgr = SessionManager()
    try:
        mgr._session_id = "win-1"
        mgr.state = SessionState.CONNECTED

        responses.add(
            responses.GET,
            f"{BASE}/session/win-1/window/handles",
            json={"value": {"error": "invalid session id", "message": "A session is either terminated or not started"}},
            status=404,
        )

        mgr._check_heartbeat()
        _join_heartbeat_threads()
        assert mgr.state == SessionState.LOST
    finally:
        mgr.close()


def _join_heartbeat_threads() -> None:
    """The heartbeat pings on a daemon thread; wait for it to settle."""
    import threading
    import time

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not any(t.is_alive() and t.daemon and t is not threading.current_thread()
                   and "_ping" in str(getattr(t, "_target", "")) for t in threading.enumerate()):
            break
        time.sleep(0.02)
    time.sleep(0.15)


# --- Custom capabilities (both platforms) -------------------------------------


def test_parse_extra_capabilities_accepts_an_object_and_rejects_the_rest():
    assert SessionManager.parse_extra_capabilities("") == {}
    assert SessionManager.parse_extra_capabilities('{"appium:showServerLogs": true}') == {
        "appium:showServerLogs": True
    }
    with pytest.raises(ValueError):
        SessionManager.parse_extra_capabilities("{not json}")
    with pytest.raises(ValueError):
        SessionManager.parse_extra_capabilities('["a", "b"]')   # must be an object


@responses.activate
def test_extra_capabilities_are_merged_into_a_mac_session(qapp):
    _mock_mac_session()
    mgr = SessionManager()
    try:
        cfg = XGenConfig(
            target_platform="mac",
            app_bundle_id="com.apple.TextEdit",
            extra_capabilities='{"appium:showServerLogs": true, "appium:arguments": ["--demo"]}',
        )
        mgr._create_session(cfg)
        caps = _sent_capabilities()
        assert caps["appium:showServerLogs"] is True
        assert caps["appium:arguments"] == ["--demo"]
    finally:
        mgr.close()


@responses.activate
def test_extra_capabilities_work_on_windows_too_including_the_jsonwp_block(qapp):
    responses.add(responses.GET, f"{BASE}/sessions", json={"value": []}, status=200)
    responses.add(
        responses.POST, f"{BASE}/session",
        json={"value": {"sessionId": "win-2", "capabilities": {}}}, status=200,
    )
    responses.add(responses.GET, f"{BASE}/session/win-2/window/handles", json={"value": []}, status=200)

    mgr = SessionManager()
    try:
        cfg = XGenConfig(target_platform="windows", extra_capabilities='{"appium:ms:waitForAppLaunch": 10}')
        mgr._create_session(cfg)
        assert _sent_capabilities()["appium:ms:waitForAppLaunch"] == 10
        # ...and mirrored into the legacy block standalone WinAppDriver reads.
        assert _sent_payload()["desiredCapabilities"]["ms:waitForAppLaunch"] == 10
    finally:
        mgr.close()


@responses.activate
def test_a_custom_capability_can_override_one_xgen_set(qapp):
    _mock_mac_session()
    mgr = SessionManager()
    try:
        cfg = XGenConfig(
            target_platform="mac",
            app_bundle_id="com.apple.TextEdit",
            extra_capabilities='{"appium:bundleId": "com.apple.Safari"}',
        )
        mgr._create_session(cfg)
        assert _sent_capabilities()["appium:bundleId"] == "com.apple.Safari"
    finally:
        mgr.close()


@responses.activate
def test_invalid_stored_capabilities_do_not_break_the_session(qapp):
    """The dialog validates, so a bad value here came from an old config file."""
    _mock_mac_session()
    mgr = SessionManager()
    try:
        cfg = XGenConfig(target_platform="mac", app_bundle_id="com.apple.TextEdit",
                         extra_capabilities="{oops")
        info = mgr._create_session(cfg)
        assert info.session_id == "mac-session-1"
        assert _sent_capabilities()["appium:bundleId"] == "com.apple.TextEdit"
    finally:
        mgr.close()


@responses.activate
def test_mac_session_defaults_to_the_frontmost_app_not_finder(qapp, monkeypatch):
    """With no app chosen, attach to whatever is frontmost rather than Finder."""
    # _create_session imports get_open_windows locally at call time, so the
    # patch has to land on the module it is imported *from*.
    import xgen.utils.window_finder as wf
    from xgen.utils.window_finder import WindowTarget

    monkeypatch.setattr(wf, "get_open_windows", lambda: [
        WindowTarget(title="TextEdit", handle_hex="0x1", hwnd=1, exe_name="com.apple.TextEdit",
                     bundle_id="com.apple.TextEdit"),
        WindowTarget(title="Safari", handle_hex="0x2", hwnd=2, exe_name="com.apple.Safari",
                     bundle_id="com.apple.Safari"),
    ], raising=False)

    _mock_mac_session()
    mgr = SessionManager()
    try:
        info = mgr._create_session(XGenConfig(target_platform="mac"))
        assert _sent_capabilities()["appium:bundleId"] == "com.apple.TextEdit"
        assert info.app_name == "TextEdit"
    finally:
        mgr.close()
