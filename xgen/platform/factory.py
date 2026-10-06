"""
The one place in xGen that checks sys.platform for native-OS behavior.

Everywhere else in the codebase asks for a PlatformBackend and uses it —
nothing else imports uiautomation, comtypes, pywin32, or calls ctypes.windll
directly. See xgen.platform.backend.PlatformBackend for the interface.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from xgen.platform.backend import PlatformBackend

_backend_instance: Optional["PlatformBackend"] = None


def get_platform_backend() -> "PlatformBackend":
    """Return the process-wide PlatformBackend singleton for the current OS."""
    global _backend_instance
    if _backend_instance is None:
        if sys.platform == "win32":
            from xgen.platform.windows_backend import WindowsBackend
            _backend_instance = WindowsBackend()
        elif sys.platform == "darwin":
            from xgen.platform.mac_backend import MacBackend
            _backend_instance = MacBackend()
        else:
            from xgen.platform.unsupported_backend import UnsupportedBackend
            _backend_instance = UnsupportedBackend()
    return _backend_instance


def reset_platform_backend_for_tests() -> None:
    """Test-only hook to force the next get_platform_backend() call to re-resolve."""
    global _backend_instance
    _backend_instance = None
