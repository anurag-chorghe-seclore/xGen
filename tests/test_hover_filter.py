"""
Tests for MainWindow._is_point_outside_xgen — the filter deciding whether a
hovered/clicked point belongs to the target app or to xGen itself.

Regression cover for a scaled-display bug: the filter also compared the
*unscaled* cursor position against xGen's *logical* window rect. Those two
spaces differ by the display's scale factor, so on a Retina Mac (or Windows
above 100%) a phantom "inside xGen" region appeared at a fraction of the
window's coordinates and silently suppressed hover there. It moved with the
window, so repositioning xGen relocated the dead zone rather than removing it.

Coordinates here are kept inside the offscreen test screen (800x800 logical),
because the filter's last step also treats anything outside the screen's
available geometry as "not the target app".
"""

from __future__ import annotations

import pytest

from xgen.config import XGenConfig
from xgen.ui.main_window import MainWindow

# xGen's own window for these tests: logical (400,300) - (700,550).
WINDOW_RECT = (400, 300, 300, 250)


@pytest.fixture
def window(qapp):
    win = MainWindow(XGenConfig(auto_connect_on_startup=False, pin_on_top=False), start_hooks=False)
    win.setGeometry(*WINDOW_RECT)
    # The filter reads a snapshot refreshed on move/resize/show, so take one
    # explicitly here rather than relying on those events firing offscreen.
    win.refresh_hover_filter_snapshot()
    yield win
    win.session_manager.close()
    win.tree_fetcher.close()
    win.close()


@pytest.fixture
def retina(monkeypatch):
    """Pretend the display is 2x, as on every Retina Mac."""
    monkeypatch.setattr(MainWindow, "_snapshot_dpr_at", lambda self, x, y: 2.0)


def test_point_whose_halved_coordinates_land_in_the_window_is_still_outside(window, retina):
    """
    The regression itself. Physical (400,300) is logical (200,150) — well clear
    of a window spanning (400,300)-(700,550). But the raw pair (400,300) sits
    exactly on that rect numerically, which used to read as "cursor is on xGen"
    and killed hover there.
    """
    assert window._is_point_outside_xgen(400, 300) is True


def test_a_point_genuinely_over_xgen_is_still_suppressed(window, retina):
    """The filter must keep doing its real job: physical (1000,800) is logical (500,400), inside."""
    assert window._is_point_outside_xgen(1000, 800) is False


def test_points_clear_of_the_window_are_outside(window, retina):
    assert window._is_point_outside_xgen(200, 200) is True     # logical (100,100)


def test_unscaled_displays_are_unaffected(window, monkeypatch):
    """At 1x the two coordinate spaces coincide, which is why this went unseen on Windows."""
    monkeypatch.setattr(MainWindow, "_snapshot_dpr_at", lambda self, x, y: 1.0)
    assert window._is_point_outside_xgen(500, 400) is False    # inside the rect
    assert window._is_point_outside_xgen(100, 100) is True     # outside it


def test_the_dead_zone_does_not_follow_the_window(window, retina):
    """
    Moving xGen used to move the phantom region with it, so a spot that worked
    before the move stopped working after. A point that is never covered by the
    window must read the same wherever the window sits.
    """
    probe = (400, 300)                       # logical (200,150)
    before = window._is_point_outside_xgen(*probe)

    window.setGeometry(100, 500, 250, 200)   # logical (100,500)-(350,700): nowhere near the probe
    window.refresh_hover_filter_snapshot()
    after = window._is_point_outside_xgen(*probe)

    assert before is True
    assert after is True
