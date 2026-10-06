"""
Unit tests for TransientCapturer (F4 snapshot, timed capture, freeze mode).
"""

import sys

import pytest
from PyQt6.QtWidgets import QApplication
from xgen.capture.transient_capture import TransientCapturer
from xgen.core.tree_cache import TreeCacheStore, WindowTreeCache
from xgen.core.tree_parser import TreeParser, UINode
from xgen.core.uia_bridge import UIAElement
from xgen.utils.rect import Rect


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_frozen_mode_toggle(qapp):
    capturer = TransientCapturer()
    assert capturer.is_frozen is False

    events = []
    capturer.freeze_state_changed.connect(lambda f: events.append(f))

    capturer.set_frozen(True)
    assert capturer.is_frozen is True
    assert events == [True]

    capturer.set_frozen(False)
    assert capturer.is_frozen is False
    assert events == [True, False]


def test_uia_list_to_tree_conversion(qapp):
    capturer = TransientCapturer()

    elements = [
        UIAElement(runtime_id="42.1", control_type="Menu", name="Context"),
        UIAElement(runtime_id="42.2", control_type="MenuItem", name="Copy", automation_id="item_copy"),
        UIAElement(runtime_id="42.3", control_type="MenuItem", name="Paste", automation_id="item_paste"),
    ]

    tree_root = capturer._uia_list_to_tree(elements)
    assert tree_root.tag == "Menu"
    assert tree_root.name == "Context"
    assert tree_root.is_transient is True
    assert len(tree_root.children) == 2
    assert tree_root.children[0].tag == "MenuItem"
    assert tree_root.children[0].automation_id == "item_copy"
    assert tree_root.children[1].automation_id == "item_paste"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="monkeypatches the real uiautomation module, which only installs on Windows",
)
def test_freeze_snapshot_no_control_emits_failure(qapp, monkeypatch):
    capturer = TransientCapturer()
    import uiautomation as auto
    monkeypatch.setattr(auto, "ControlFromPoint", lambda x, y: None)

    failed_reasons = []
    capturer.transient_failed.connect(lambda r: failed_reasons.append(r))

    capturer.freeze_snapshot(100, 100)
    assert len(failed_reasons) == 1
    assert "No control found" in failed_reasons[0]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="monkeypatches the real uiautomation module, which only installs on Windows",
)
def test_freeze_snapshot_desktop_emits_failure(qapp, monkeypatch):
    capturer = TransientCapturer()
    import uiautomation as auto

    class MockControl:
        Name = "Desktop"
        ClassName = "#32769"

    monkeypatch.setattr(auto, "ControlFromPoint", lambda x, y: MockControl())

    failed_reasons = []
    capturer.transient_failed.connect(lambda r: failed_reasons.append(r))

    capturer.freeze_snapshot(100, 100)
    assert len(failed_reasons) == 1
    assert "desktop background" in failed_reasons[0]


def test_timed_capture_countdown_and_cancel(qapp):
    capturer = TransientCapturer()
    ticks = []
    capturer.timed_capture_tick.connect(lambda s: ticks.append(s))

    capturer.start_timed_capture(delay_seconds=3)
    assert ticks == [3]
    assert capturer._seconds_left == 3

    capturer.cancel_timed_capture()
    assert capturer._seconds_left == 0
    assert capturer._countdown_timer is None
