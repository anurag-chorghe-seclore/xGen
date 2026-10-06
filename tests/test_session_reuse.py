"""
Tests for which already-running Appium sessions xGen will adopt.

Regression cover for a real Windows bug: connecting to Desktop Root silently
latched onto a stale session left behind by another automation tool and
presented it as Desktop Root, so the tree belonged to some other app. Two
flaws stacked in one expression —

    existing_is_root = existing_app in ("Root", "", "root")

an *absent* app capability counted as Root, and a session explicitly scoped to
a top-level window was never excluded from being treated as Root either.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication

from xgen.core.driver_dialect import MAC2_DIALECT, WINDOWS_DIALECT
from xgen.core.session_manager import SessionManager


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _find(sessions, **kwargs):
    return SessionManager._find_reusable_session(sessions, WINDOWS_DIALECT, **kwargs)


# --- The reported bug ---------------------------------------------------------


def test_session_with_no_app_capability_is_not_treated_as_desktop_root():
    """The reported bug: an unidentifiable stale session was adopted as Root."""
    stale = [{"id": "stale-1", "capabilities": {"platformName": "Windows"}}]
    assert _find(stale) is None


def test_window_scoped_session_is_never_adopted_as_desktop_root():
    """A session pinned to a top-level window is definitively not the desktop."""
    other_tool = [{
        "id": "stale-2",
        "capabilities": {"platformName": "Windows", "appium:appTopLevelWindow": "0x000E07FA"},
    }]
    assert _find(other_tool) is None


def test_app_scoped_session_is_not_adopted_as_desktop_root():
    app_session = [{
        "id": "stale-3",
        "capabilities": {"platformName": "Windows", "appium:app": r"C:\Windows\System32\notepad.exe"},
    }]
    assert _find(app_session) is None


def test_empty_capabilities_are_never_enough_to_reuse():
    assert _find([{"id": "s", "capabilities": {}}]) is None
    assert _find([{"id": "s"}]) is None


# --- Reuse that should still happen -------------------------------------------


def test_explicit_root_session_is_reused_for_desktop_root():
    root_session = [{"id": "root-1", "capabilities": {"platformName": "Windows", "appium:app": "Root"}}]
    found = _find(root_session)
    assert found is not None and found[0] == "root-1"


def test_matching_session_is_found_even_when_it_is_not_the_first():
    """Previously only sessions[0] was ever considered."""
    sessions = [
        {"id": "other", "capabilities": {"platformName": "Windows", "appium:app": r"C:\other.exe"}},
        {"id": "wanted", "capabilities": {"platformName": "Windows", "appium:app": "Root"}},
    ]
    found = _find(sessions)
    assert found is not None and found[0] == "wanted"


def test_same_app_path_is_reused():
    sessions = [{"id": "np", "capabilities": {"platformName": "Windows", "appium:app": r"C:\app.exe"}}]
    found = _find(sessions, app_target=r"C:\app.exe")
    assert found is not None and found[0] == "np"


def test_same_window_handle_is_reused_across_handle_spellings():
    sessions = [{
        "id": "win",
        "capabilities": {"platformName": "Windows", "appium:appTopLevelWindow": "0x000E07FA"},
    }]
    found = _find(sessions, app_window="919546")  # same handle in decimal
    assert found is not None and found[0] == "win"


def test_a_root_session_is_not_reused_when_a_specific_app_was_asked_for():
    sessions = [{"id": "root", "capabilities": {"platformName": "Windows", "appium:app": "Root"}}]
    assert _find(sessions, app_target=r"C:\app.exe") is None


# --- Cross-driver safety ------------------------------------------------------


def test_a_mac_session_is_never_reused_for_a_windows_request():
    mac_session = [{
        "id": "mac-1",
        "capabilities": {"platformName": "mac", "appium:automationName": "mac2",
                         "appium:bundleId": "com.apple.TextEdit"},
    }]
    assert _find(mac_session) is None


def test_mac_reuse_requires_an_exact_bundle_match():
    sessions = [{
        "id": "mac-1",
        "capabilities": {"platformName": "mac", "appium:automationName": "mac2",
                         "appium:bundleId": "com.apple.TextEdit"},
    }]
    assert SessionManager._find_reusable_session(
        sessions, MAC2_DIALECT, bundle_id="com.apple.Safari"
    ) is None
    found = SessionManager._find_reusable_session(
        sessions, MAC2_DIALECT, bundle_id="com.apple.TextEdit"
    )
    assert found is not None and found[0] == "mac-1"


def test_mac_request_with_no_target_does_not_adopt_an_arbitrary_session():
    """Mirrors the Windows rule: "nothing specified" must not match anything."""
    sessions = [{
        "id": "mac-1",
        "capabilities": {"platformName": "mac", "appium:automationName": "mac2",
                         "appium:bundleId": "com.apple.TextEdit"},
    }]
    assert SessionManager._find_reusable_session(sessions, MAC2_DIALECT) is None
