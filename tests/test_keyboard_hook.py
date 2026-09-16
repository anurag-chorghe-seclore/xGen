"""
Tests for GlobalKeyHook (cross-platform shortcuts) and Qt-native Pinning.
"""

from __future__ import annotations

from unittest.mock import patch
from PyQt6.QtCore import Qt
from pynput import keyboard

from xgen.capture.keyboard_hook import GlobalKeyHook
from xgen.config import XGenConfig
from xgen.core.tree_fetcher import TreeFetcher
from xgen.ui.main_window import MainWindow


def test_ctrl_r_cross_platform_detection(qapp):
    """Test that Ctrl+R / Cmd+R emits signal cross-platform and obeys 0.5s debounce."""
    hook = GlobalKeyHook()
    events = []
    hook.ctrl_r_pressed.connect(lambda: events.append(True))

    r_key = keyboard.KeyCode(char="r")
    ctrl_key = keyboard.Key.ctrl
    cmd_key = keyboard.Key.cmd

    # 1. Plain 'r' without modifier should not trigger
    hook._on_press(r_key)
    assert len(events) == 0

    # 2. Ctrl + 'r' triggers
    hook._on_press(ctrl_key)
    hook._on_press(r_key)
    hook._on_release(r_key)
    hook._on_release(ctrl_key)
    assert len(events) == 1

    # 3. Rapid repeat (< 0.5s) is debounced
    hook._on_press(ctrl_key)
    hook._on_press(r_key)
    hook._on_release(r_key)
    hook._on_release(ctrl_key)
    assert len(events) == 1

    # 4. After debounce period, Cmd + 'r' (macOS) triggers
    hook._last_ctrl_r_time -= 0.6
    hook._on_press(cmd_key)
    hook._on_press(r_key)
    hook._on_release(r_key)
    hook._on_release(cmd_key)
    assert len(events) == 2

    # 5. Windows control character fallback (\\x12) triggers
    hook._last_ctrl_r_time -= 0.6
    hook._on_press(keyboard.KeyCode(char="\x12"))
    assert len(events) == 3


def test_main_window_pin_toggle_qt_native(qapp):
    """Verify MainWindow uses pure cross-platform Qt window flags for pinning."""
    cfg = XGenConfig(auto_connect_on_startup=False, pin_on_top=False)
    win = MainWindow(cfg, start_hooks=False)
    try:
        # Initially not pinned
        assert not bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

        # Pin ON
        win.toolbar.btn_pin.click()
        assert bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

        # Pin OFF
        win.toolbar.btn_pin.click()
        assert not bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    finally:
        win.session_manager.close()
        win.tree_fetcher.close()
        win.close()


def test_main_window_ctrl_r_triggers_fetch(qapp):
    """Verify key_hook.ctrl_r_pressed triggers tree_fetcher.fetch_full."""
    with patch.object(TreeFetcher, "fetch_full") as mock_fetch:
        cfg = XGenConfig(auto_connect_on_startup=False, pin_on_top=False)
        win = MainWindow(cfg, start_hooks=False)
        try:
            win.key_hook.ctrl_r_pressed.emit()
            mock_fetch.assert_called_once()
        finally:
            win.session_manager.close()
            win.tree_fetcher.close()
            win.close()
