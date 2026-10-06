"""
Regression cover for concurrency bugs found in a threading audit.

xGen runs HTTP on QThread workers, spawns bare daemon threads for session
deletion and heartbeat pings, and receives global mouse/keyboard events on
pynput's own listener threads — so "which thread is this running on" is a real
question for most of the shared state here.
"""

from __future__ import annotations

import threading

import pytest
from xgen.config import XGenConfig
from xgen.core.driver_dialect import (
    MAC2_DIALECT,
    WINDOWS_DIALECT,
    DriverDialectStore,
)
from xgen.core.session_manager import SessionManager, SessionState
from xgen.ui.main_window import MainWindow


# --- Session lifecycle --------------------------------------------------------


def test_aborting_an_in_flight_connect_clears_the_session_id(qapp):
    """
    _create_session sets _session_id on the worker thread. If the user
    disconnects while that is still running, the session is deleted server-side
    — but everything that gates on "do we have a session" checks _session_id,
    not state, so leaving it set means later calls fire at a deleted session and
    surface as a confusing HTTP 404.
    """
    mgr = SessionManager()
    try:
        mgr._disconnect_requested = True
        mgr._session_id = "doomed-session"

        info = type("Info", (), {"session_id": "doomed-session"})()
        mgr._on_connect_success(info)

        assert mgr.session_id is None
        assert mgr.state == SessionState.DISCONNECTED
    finally:
        mgr.close()


def test_a_late_heartbeat_cannot_resurrect_a_deliberately_closed_session(qapp):
    """
    Pings run on daemon threads with a 10s timeout, so one can land after the
    user disconnected. It used to flip the UI to "Session lost" afterwards.
    """
    mgr = SessionManager()
    try:
        mgr._session_id = "s1"
        mgr.state = SessionState.CONNECTED
        mgr._disconnect_requested = True        # user disconnected meanwhile

        mgr._report_session_lost()
        assert mgr.state == SessionState.CONNECTED, "closed session must not be reported lost"

        # With no disconnect pending, a genuine failure still reports.
        mgr._disconnect_requested = False
        mgr._report_session_lost()
        assert mgr.state == SessionState.LOST
    finally:
        mgr.close()


def test_heartbeat_does_not_probe_once_a_disconnect_is_pending(qapp):
    mgr = SessionManager()
    try:
        mgr._session_id = "s1"
        mgr.state = SessionState.CONNECTED
        mgr._disconnect_requested = True

        probed = []
        mgr._http_session.get = lambda *a, **k: probed.append(a)  # type: ignore[assignment]
        mgr._check_heartbeat()
        assert probed == []
    finally:
        mgr.close()


# --- Dialect singleton --------------------------------------------------------


def test_dialect_store_stays_consistent_under_concurrent_access():
    """
    set_active is called from the GUI thread and from the session worker, while
    the fetcher thread reads it mid-parse. A read must always return a whole
    dialect, never a half-written or missing one.
    """
    store = DriverDialectStore.instance()
    store.reset()
    seen = []
    stop = threading.Event()

    def _writer():
        while not stop.is_set():
            store.set_active(MAC2_DIALECT)
            store.set_active(WINDOWS_DIALECT)

    def _reader():
        for _ in range(2000):
            seen.append(store.active)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    _reader()
    stop.set()
    writer.join(timeout=2.0)

    assert seen, "reader observed nothing"
    assert all(d in (MAC2_DIALECT, WINDOWS_DIALECT) for d in seen)
    store.reset()


# --- Hover filter thread safety ----------------------------------------------


def test_hover_filter_is_safe_to_call_off_the_gui_thread(qapp):
    """
    capture/mouse_hook.py calls this filter from pynput's listener thread for
    every mouse event while Inspect Mode is on. It used to read winId(),
    frameGeometry() and QScreen from there — all GUI-thread-only, and on macOS
    winId() reaches AppKit, which is outright illegal off the main thread. It
    must now answer purely from the cached snapshot.
    """
    win = MainWindow(XGenConfig(auto_connect_on_startup=False, pin_on_top=False), start_hooks=False)
    try:
        win.setGeometry(400, 300, 300, 250)
        win.refresh_hover_filter_snapshot()

        results: list = []
        errors: list = []

        def _call_from_hook_thread():
            try:
                for _ in range(50):
                    results.append(win._is_point_outside_xgen(100, 100))
            except Exception as e:      # pragma: no cover - the thing we're guarding against
                errors.append(e)

        t = threading.Thread(target=_call_from_hook_thread)
        t.start()
        t.join(timeout=5.0)

        assert not errors, f"filter raised off the GUI thread: {errors}"
        assert len(results) == 50
        assert all(isinstance(r, bool) for r in results)
    finally:
        win.session_manager.close()
        win.tree_fetcher.close()
        win.close()
