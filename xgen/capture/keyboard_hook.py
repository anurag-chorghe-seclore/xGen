"""
Global OS-Level Keyboard Hook.
Captures F3 (Inspect), F4 (Freeze Snapshot), Esc (Cancel), and Ctrl+R (Refresh)
globally across platforms even when external menus or other applications have focus.
"""

from __future__ import annotations

import logging
from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal
from pynput import keyboard

import time

logger = logging.getLogger("xgen.keyboard_hook")


class GlobalKeyHook(QObject):
    """
    Global low-level keyboard listener forwarding system-wide shortcuts to Qt signals.
    """
    f3_pressed = pyqtSignal()
    f4_pressed = pyqtSignal()
    esc_pressed = pyqtSignal()
    ctrl_r_pressed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._listener: Optional[keyboard.Listener] = None
        self._is_running = False
        self._last_f3_time = 0.0
        self._last_f4_time = 0.0
        self._last_esc_time = 0.0
        self._last_ctrl_r_time = 0.0
        self._modifier_active = False

    def start(self) -> None:
        """Start global keyboard hook in background thread."""
        if self._is_running:
            return

        try:
            self._listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
                daemon=True
            )
            self._listener.start()
            self._is_running = True
            logger.info("Global keyboard hook started (F3, F4, Esc, Ctrl+R).")
        except Exception as e:
            logger.warning("Could not start global keyboard hook: %s", e)

    def stop(self) -> None:
        """Stop global keyboard hook."""
        if not self._is_running:
            return

        self._is_running = False
        self._modifier_active = False
        if self._listener:
            try:
                self._listener.stop()
            except Exception as e:
                logger.debug("Keyboard listener stop note: %s", e)
            self._listener = None
        logger.info("Global keyboard hook stopped.")

    def _on_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        """Track modifier key releases cross-platform (Ctrl, Cmd)."""
        if key in (
            keyboard.Key.ctrl,
            keyboard.Key.ctrl_l,
            keyboard.Key.ctrl_r,
            keyboard.Key.cmd,
            keyboard.Key.cmd_l,
            keyboard.Key.cmd_r,
        ):
            self._modifier_active = False

    def _is_refresh_shortcut(self, key: keyboard.Key | keyboard.KeyCode | None) -> bool:
        """Check for Ctrl+R (or Cmd+R on macOS) cross-platform."""
        char = getattr(key, "char", None)
        # 1. Standard cross-platform: 'r' / 'R' while Ctrl or Cmd modifier is active
        if self._modifier_active and char and char.lower() == "r":
            return True
        # 2. KeyCode comparison while modifier is active
        if self._modifier_active and key == keyboard.KeyCode.from_char("r"):
            return True
        # 3. Control character fallback (ASCII 18 / DC2 emitted when Ctrl is held)
        if char == "\x12":
            return True
        return False

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        """Invoked from pynput thread on any system key press with debounce protection."""
        if key in (
            keyboard.Key.ctrl,
            keyboard.Key.ctrl_l,
            keyboard.Key.ctrl_r,
            keyboard.Key.cmd,
            keyboard.Key.cmd_l,
            keyboard.Key.cmd_r,
        ):
            self._modifier_active = True
            return

        now = time.monotonic()
        try:
            if key == keyboard.Key.f3:
                if now - self._last_f3_time >= 0.35:
                    self._last_f3_time = now
                    logger.debug("Global F3 detected.")
                    self.f3_pressed.emit()
            elif key == keyboard.Key.f4:
                if now - self._last_f4_time >= 0.35:
                    self._last_f4_time = now
                    logger.debug("Global F4 detected.")
                    self.f4_pressed.emit()
            elif key == keyboard.Key.esc:
                if now - self._last_esc_time >= 0.35:
                    self._last_esc_time = now
                    logger.debug("Global Esc detected.")
                    self.esc_pressed.emit()
            elif self._is_refresh_shortcut(key):
                if now - self._last_ctrl_r_time >= 0.5:
                    self._last_ctrl_r_time = now
                    logger.debug("Global Refresh (Ctrl+R) detected.")
                    self.ctrl_r_pressed.emit()
        except Exception as e:
            logger.debug("Keyboard hook callback error: %s", e)
