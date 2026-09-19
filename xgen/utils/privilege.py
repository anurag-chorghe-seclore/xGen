"""
Privilege & Elevation utilities.

Thin, stable wrappers over the current PlatformBackend (see
xgen/platform/windows_backend.py for the real Windows implementation).
"""

from __future__ import annotations


def is_running_as_admin() -> bool:
    """Return True if the current process is running with elevated/administrator privileges."""
    from xgen.platform.factory import get_platform_backend
    return get_platform_backend().is_running_as_admin()


def relaunch_as_admin() -> bool:
    """
    Relaunch the current application elevated (UAC prompt on Windows).
    Returns True if the relaunch was initiated (the caller should then exit).
    """
    from xgen.platform.factory import get_platform_backend
    return get_platform_backend().relaunch_as_admin()
