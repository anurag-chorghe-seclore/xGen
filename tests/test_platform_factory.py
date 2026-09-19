"""
Tests for the platform backend factory — the single sys.platform switch
point that selects WindowsBackend vs. UnsupportedBackend.
"""

from __future__ import annotations

import sys

import pytest

from xgen.platform.factory import get_platform_backend, reset_platform_backend_for_tests
from xgen.platform.unsupported_backend import UnsupportedBackend
from xgen.platform.windows_backend import WindowsBackend


@pytest.fixture(autouse=True)
def _reset_backend_singleton():
    reset_platform_backend_for_tests()
    yield
    reset_platform_backend_for_tests()


def test_returns_windows_backend_on_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    backend = get_platform_backend()
    assert isinstance(backend, WindowsBackend)


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_returns_unsupported_backend_off_windows(monkeypatch, plat):
    monkeypatch.setattr(sys, "platform", plat)
    backend = get_platform_backend()
    assert isinstance(backend, UnsupportedBackend)


def test_singleton_is_cached_until_reset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    first = get_platform_backend()
    second = get_platform_backend()
    assert first is second

    reset_platform_backend_for_tests()
    monkeypatch.setattr(sys, "platform", "win32")
    third = get_platform_backend()
    assert isinstance(third, WindowsBackend)
    assert third is not first


def test_unsupported_backend_is_inert():
    backend = UnsupportedBackend()
    assert backend.element_from_point(0, 0) is None
    # get_open_windows() always includes the synthetic desktop-root entry
    # (see xgen.utils.window_finder._DESKTOP_ROOT); it never lists real windows.
    windows = backend.get_open_windows()
    assert len(windows) == 1
    assert windows[0].is_root is True
    assert backend.window_from_point(0, 0) is None
    assert backend.get_process_id_for_window(1) is None
    assert backend.move_cursor_to(0, 0) is False
    assert backend.is_accessibility_available() is False
    # No-op styling/lifecycle calls must not raise on a bare object.
    backend.apply_click_through(object())
    backend.enforce_topmost(object())
    backend.init_dpi_awareness()
    backend.set_app_user_model_id("anything")
