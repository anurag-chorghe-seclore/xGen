"""
Unit tests for window_finder utility.
"""

import os
import sys
import pytest
from xgen.utils.window_finder import WindowTarget, get_open_windows


def test_desktop_root_leads_the_list_only_where_the_platform_has_one():
    """Desktop Root is a Windows concept, not a universal one.

    WinAppDriver's Root target inspects every application at once, so there it
    is always entry 0. An Appium Mac2 session attaches to a single application
    and macOS has no desktop-wide equivalent, so a root entry there would
    promise something no session can deliver — and picking it would silently
    land the user on Finder. The backend states which world it is in; this
    asserts the matching contract rather than assuming Windows.
    """
    from xgen.platform.factory import get_platform_backend

    windows = get_open_windows()
    if get_platform_backend().supports_desktop_root():
        assert len(windows) >= 1
        root = windows[0]
        assert root.is_root is True
        assert root.handle_hex == ""
        assert root.hwnd == 0
        assert "Desktop Root" in root.display_label()
    else:
        assert not any(w.is_root for w in windows), (
            "a platform without a desktop-wide target must not offer a root entry"
        )


def test_window_target_display_label():
    root = WindowTarget(
        title="Desktop Root (Entire OS)",
        handle_hex="",
        hwnd=0,
        exe_name="",
        is_root=True
    )
    assert root.display_label() == "Desktop Root (Entire OS)"

    app_target = WindowTarget(
        title="My Application",
        handle_hex="0x0012ABCD",
        hwnd=1223629,
        exe_name="myapp.exe"
    )
    assert app_target.display_label() == "My Application [myapp.exe]"

    no_exe_target = WindowTarget(
        title="Untitled",
        handle_hex="0x0001",
        hwnd=1,
        exe_name=""
    )
    assert no_exe_target.display_label() == "Untitled"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows specific ctypes enumeration")
def test_own_pid_excluded():
    windows = get_open_windows()
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    own_pid = os.getpid()

    for w in windows:
        if w.is_root:
            continue
        pid = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(w.hwnd, ctypes.byref(pid))
        assert pid.value != own_pid, f"Window {w.title} ({w.handle_hex}) belongs to own PID {own_pid}"


def test_normalize_handle():
    from xgen.utils.window_finder import normalize_handle

    assert normalize_handle("0x000E07FA") == 919546
    assert normalize_handle("0xe07fa") == 919546
    assert normalize_handle("919546") == 919546
    assert normalize_handle(919546) == 919546
    assert normalize_handle("") is None
    assert normalize_handle("Root") is None
    assert normalize_handle(None) is None
    assert normalize_handle(0) is None
    assert normalize_handle("invalid") is None
