"""
Unit tests for InspectMode, OverlayWindow, and UIABridge components.
"""

import pytest
from PyQt6.QtWidgets import QApplication
from xgen.capture.inspect_mode import InspectMode
from xgen.capture.mouse_hook import MouseHook
from xgen.capture.overlay_window import OverlayWindow
from xgen.config import XGenConfig
from xgen.core.uia_bridge import UIAElement
from xgen.utils.rect import Rect


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_overlay_window_geometry(qapp, monkeypatch):
    # move_to() converts the (physical-pixel) target rect to Qt logical coordinates
    # via physical_to_logical_rect()/get_screen_dpr_at() to correctly place the
    # overlay on a scaled display (see xgen/utils/dpi.py). That conversion is
    # environment-dependent: on an unscaled display (e.g. this sandbox, or a
    # monitor at 100% Windows scaling) it's a no-op, but on any other scaling
    # (125%, 150%, ...) it legitimately shrinks/grows the logical geometry, which
    # would make this test's exact-pixel assertions flaky depending on the
    # machine's actual display settings. Pin the scale factor to 1.0 so this test
    # verifies the insetting math on its own, independent of the host's DPI.
    monkeypatch.setattr("xgen.utils.dpi.get_screen_dpr_at", lambda x, y: 1.0)

    overlay = OverlayWindow()
    target_rect = Rect(left=100, top=150, right=300, bottom=250)

    overlay.move_to(target_rect)
    assert overlay.isVisible() is True
    # Overlay insets by 2px on each side (width + 4, height + 4)
    geom = overlay.geometry()
    assert geom.width() == target_rect.width + 4
    assert geom.height() == target_rect.height + 4

    overlay.hide_overlay()
    assert overlay.isVisible() is False


def test_overlay_multi_monitor_negative_coords(qapp):
    overlay = OverlayWindow()
    # Secondary monitor positioned to the left of primary monitor
    target_rect = Rect(left=-1800, top=100, right=-1600, bottom=200)

    overlay.move_to(target_rect)
    assert overlay.isVisible() is True
    geom = overlay.geometry()
    # Ensure left coordinate is negative and preserved without being clamped to 0
    assert geom.left() < 0
    overlay.hide_overlay()


def test_inspect_mode_state_transitions(qapp):
    cfg = XGenConfig()
    overlay = OverlayWindow()
    hook = MouseHook()
    inspect = InspectMode(cfg, overlay=overlay, mouse_hook=hook)

    assert inspect.is_active is False

    # Activate
    inspect.activate()
    assert inspect.is_active is True

    # Simulate element click
    sample_el = UIAElement(
        runtime_id="42.999",
        control_type="Button",
        automation_id="btn_test",
        bounding_rect=Rect(10, 10, 100, 50)
    )
    inspect._last_uia_el = sample_el

    clicked_elements = []
    inspect.element_clicked.connect(lambda el, x, y: clicked_elements.append(el))

    inspect._on_mouse_click(50, 25)
    # Continuous inspect mode stays active until explicitly toggled off
    assert inspect.is_active is True
    assert len(clicked_elements) == 1
    assert clicked_elements[0].automation_id == "btn_test"

    inspect.deactivate()
    assert inspect.is_active is False


def test_global_key_hook(qapp):
    from xgen.capture.keyboard_hook import GlobalKeyHook
    from pynput import keyboard

    hook = GlobalKeyHook()
    f3_events = []
    f4_events = []
    esc_events = []
    ctrl_r_events = []

    hook.f3_pressed.connect(lambda: f3_events.append(True))
    hook.f4_pressed.connect(lambda: f4_events.append(True))
    hook.esc_pressed.connect(lambda: esc_events.append(True))
    hook.ctrl_r_pressed.connect(lambda: ctrl_r_events.append(True))

    # Test key event routing
    hook._on_press(keyboard.Key.f3)
    hook._on_press(keyboard.Key.f4)
    hook._on_press(keyboard.Key.esc)
    hook._on_press(keyboard.KeyCode(char="\x12"))

    assert len(f3_events) == 1
    assert len(f4_events) == 1
    assert len(esc_events) == 1
    assert len(ctrl_r_events) == 1
