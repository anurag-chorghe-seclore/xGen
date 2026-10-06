"""
xGen Platform Abstraction Layer.

Every native-OS call in xGen (native accessibility/UIA queries, window
enumeration, overlay window styling, DPI awareness, privilege elevation,
cursor control) goes through a single `PlatformBackend` interface, obtained
via `xgen.platform.factory.get_platform_backend()`.

Windows (`WindowsBackend`, in `windows_backend.py`) and macOS
(`MacBackend`, in `mac_backend.py`, talking to Accessibility/Quartz/AppKit
via PyObjC) each have a real implementation. `UnsupportedBackend` is the
safe no-op used for any other `sys.platform` (e.g. Linux) — it exists
purely so importing and constructing xGen's objects never crashes there;
it does not provide a working experience on those platforms, and building
one is out of scope.

Every native-OS touchpoint goes through this one seam
(`xgen.platform.factory.get_platform_backend()`) and nowhere else — no
module outside `xgen/platform/` imports a platform-specific package
(uiautomation, pywin32, PyObjC, ...) or branches on `sys.platform`/
`platform.system()` directly. Keeping platform-dependent code decoupled
from the rest of xGen this way is a standing project rule, not just a
one-time refactor.
"""
