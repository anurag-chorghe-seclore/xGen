"""
Backward-compatible shim.

The native UIA COM bridge now lives in xgen.platform.windows_backend.WindowsBackend,
reached through xgen.platform.factory.get_platform_backend() — nothing in this
codebase should need to import uiautomation directly any more.

UIAElement / UIABridge are kept importable from here, under their original
names, for anything not yet updated to the new xgen.platform API.
"""

from __future__ import annotations

from typing import List, Optional

from xgen.platform.backend import NativeElement as UIAElement
from xgen.platform.factory import get_platform_backend

__all__ = ["UIAElement", "UIABridge"]


class UIABridge:
    """Static-method facade over the current PlatformBackend's accessibility methods."""

    @staticmethod
    def initialize() -> None:
        get_platform_backend().initialize_accessibility()

    @staticmethod
    def element_from_point(x: int, y: int) -> Optional[UIAElement]:
        return get_platform_backend().element_from_point(x, y)

    @staticmethod
    def walk_subtree(
        root_ctrl: object = None,
        root_element: Optional[UIAElement] = None,
        max_depth: int = 20,
        max_elements: int = 250,
    ) -> List[UIAElement]:
        # root_ctrl (a raw native Control) was only ever used internally by the
        # old F4 capture code, which now lives entirely in WindowsBackend; the
        # public surface here only supports the root_element form.
        return get_platform_backend().walk_subtree(root_element=root_element, max_depth=max_depth, max_elements=max_elements)
