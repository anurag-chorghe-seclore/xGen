"""
Tests for MouseHook's native click-suppression wiring.

Covers the generic _platform_intercept path added for macOS's darwin_intercept
(via a fake PlatformBackend, so no real Quartz/pynput native hook is ever
installed) and the _native_filter_active guard that stops the plain on_click
fallback from double-emitting whenever a native filter is doing the real work
— the same bug class Issue 1 (Refresh-button ANR) came from: a UI-thread-
visible behavior change caused by assuming Windows' exact wiring generalizes
to a second backend without checking.
"""

from __future__ import annotations

from typing import Optional, Tuple

import pytest

from xgen.capture.mouse_hook import MouseHook


class _FakeMacLikeBackend:
    """Minimal stand-in for MacBackend's two input-suppression methods."""

    def __init__(self, left_down_type: object = "down", left_up_type: object = "up"):
        self._left_down_type = left_down_type
        self._left_up_type = left_up_type

    def mouse_filter_kwarg_name(self) -> Optional[str]:
        return "darwin_intercept"

    def decode_mouse_button_event(self, event_type: object, native_event: object) -> Optional[Tuple[int, int, bool]]:
        if event_type == self._left_down_type:
            return (native_event[0], native_event[1], True)
        if event_type == self._left_up_type:
            return (native_event[0], native_event[1], False)
        return None  # irrelevant event type (e.g. right-click, mouse-moved)


@pytest.fixture
def hook(qapp):
    h = MouseHook()
    yield h
    h.stop()


def test_platform_intercept_suppresses_click_outside_xgen(hook):
    """A left-button-down outside xGen's own window is suppressed and emits left_clicked."""
    backend = _FakeMacLikeBackend()
    hook._is_active = True
    hook._filter = lambda x, y: True  # True == "outside xGen" per MouseHook.start()'s docstring

    events = []
    hook.left_clicked.connect(lambda x, y: events.append((x, y)))

    result = _invoke_intercept_with_backend(hook, backend, "down", (150, 240))

    assert result is None  # None == suppressed, per pynput's darwin_intercept contract
    assert events == [(150, 240)]


def test_platform_intercept_passes_through_click_inside_xgen(hook):
    """A click inside xGen's own window geometry must NOT be suppressed and must NOT emit."""
    backend = _FakeMacLikeBackend()
    hook._is_active = True
    hook._filter = lambda x, y: False  # False == "inside xGen" -> let it through

    events = []
    hook.left_clicked.connect(lambda x, y: events.append((x, y)))

    sentinel_event = (10, 20)
    result = _invoke_intercept_with_backend(hook, backend, "down", sentinel_event)

    assert result is sentinel_event  # pass-through: must return the original native_event object
    assert events == []


def test_platform_intercept_ignores_irrelevant_event_types(hook):
    """An event type the backend doesn't recognize (e.g. right-click) passes through untouched."""
    backend = _FakeMacLikeBackend()
    hook._is_active = True
    hook._filter = lambda x, y: True

    events = []
    hook.left_clicked.connect(lambda x, y: events.append((x, y)))

    sentinel_event = (5, 5)
    result = _invoke_intercept_with_backend(hook, backend, "right-click", sentinel_event)

    assert result is sentinel_event
    assert events == []


def test_platform_intercept_noop_while_inactive(hook):
    """Once stop()/deactivate has run, the native filter must stop acting even if pynput calls it once more."""
    backend = _FakeMacLikeBackend()
    hook._is_active = False

    events = []
    hook.left_clicked.connect(lambda x, y: events.append((x, y)))

    sentinel_event = (1, 1)
    result = _invoke_intercept_with_backend(hook, backend, "down", sentinel_event)

    assert result is sentinel_event
    assert events == []


def test_on_click_fallback_is_disabled_whenever_a_native_filter_is_active(hook):
    """
    Regression test for the double-emission bug: once _native_filter_active is
    True (win32_event_filter OR a backend's darwin_intercept), pynput's separate
    on_click callback must be a no-op, since it fires in addition to, not instead
    of, the native filter.
    """
    from pynput import mouse as pynput_mouse

    events = []
    hook.left_clicked.connect(lambda x, y: events.append((x, y)))

    hook._is_active = True
    hook._filter = lambda x, y: True
    hook._native_filter_active = True

    hook._on_click(99, 99, pynput_mouse.Button.left, True)
    assert events == [], "on_click must stay silent while a native filter is active"

    hook._native_filter_active = False
    hook._on_click(99, 99, pynput_mouse.Button.left, True)
    assert events == [(99, 99)], "on_click should still work on platforms with no native filter"


def _invoke_intercept_with_backend(hook: MouseHook, backend: object, event_type: object, native_event: object):
    """Calls MouseHook._platform_intercept with get_platform_backend() patched for this one call."""
    import xgen.platform.factory as factory_module
    original = factory_module.get_platform_backend
    factory_module.get_platform_backend = lambda: backend
    try:
        return hook._platform_intercept(event_type, native_event)
    finally:
        factory_module.get_platform_backend = original
