"""
xGen Platform Abstraction Layer.

Every native-OS call in xGen (native accessibility/UIA queries, window
enumeration, overlay window styling, DPI awareness, privilege elevation,
cursor control) goes through a single `PlatformBackend` interface, obtained
via `xgen.platform.factory.get_platform_backend()`.

Today only Windows has a real implementation (`WindowsBackend`, in
`windows_backend.py`) — that is the only platform xGen supports end to end.
`UnsupportedBackend` exists purely so importing and constructing xGen's
objects never crashes on a non-Windows `sys.platform`; it does not provide
a working non-Windows experience, and building one is explicitly out of
scope for this package (see the project's Windows-Guard-Platform-
Abstraction-Plan for the decision record).
"""
