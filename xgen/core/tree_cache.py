"""
In-memory multi-window UI Tree Cache Store.
Keeps parsed UINode trees and compiled lxml trees alive in memory for instant XPath verification.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from lxml import etree
from PyQt6.QtCore import QReadWriteLock

from xgen.core.tree_parser import TreeParser, UINode

logger = logging.getLogger("xgen.cache")


@dataclass
class WindowTreeCache:
    """Cached UI tree state for a single top-level window handle."""
    window_handle: str
    raw_xml: str
    parsed_root: UINode
    lxml_tree: etree._Element                        # Live lxml element tree for fast XPath queries
    node_map: Dict[str, UINode] = field(default_factory=dict)
    fetched_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    is_stale: bool = False
    partial_scopes: Dict[str, UINode] = field(default_factory=dict)

    @property
    def age_seconds(self) -> int:
        return int((datetime.datetime.now() - self.fetched_at).total_seconds())


class TreeCacheStore:
    """
    Thread-safe storage of per-window UI trees.
    Protected by QReadWriteLock for safe multi-threaded reads during XPath verification.
    """
    _instance: Optional[TreeCacheStore] = None

    def __init__(self):
        self._lock = QReadWriteLock()
        self._caches: Dict[str, WindowTreeCache] = {}
        self._active_handle: str = ""

    @classmethod
    def instance(cls) -> TreeCacheStore:
        if cls._instance is None:
            cls._instance = TreeCacheStore()
        return cls._instance

    def set(self, handle: str, cache: WindowTreeCache) -> None:
        """Store or overwrite tree cache for the window handle."""
        self._lock.lockForWrite()
        try:
            self._caches[handle] = cache
            if not self._active_handle:
                self._active_handle = handle
            logger.debug("Tree cached for handle %s (storing %d nodes)", handle, TreeParser.node_count(cache.parsed_root))
        finally:
            self._lock.unlock()

    def get(self, handle: str) -> Optional[WindowTreeCache]:
        """Retrieve tree cache for a specific window handle."""
        self._lock.lockForRead()
        try:
            return self._caches.get(handle)
        finally:
            self._lock.unlock()

    def get_active(self) -> Optional[WindowTreeCache]:
        """Retrieve cache for the currently active window."""
        self._lock.lockForRead()
        try:
            if self._active_handle in self._caches:
                return self._caches[self._active_handle]
            # Fallback to first available cache if active handle not found
            if self._caches:
                return next(iter(self._caches.values()))
            return None
        finally:
            self._lock.unlock()

    def set_active_handle(self, handle: str) -> None:
        """Set the currently active window handle."""
        self._lock.lockForWrite()
        try:
            self._active_handle = handle
        finally:
            self._lock.unlock()

    @property
    def active_handle(self) -> str:
        self._lock.lockForRead()
        try:
            return self._active_handle
        finally:
            self._lock.unlock()

    def mark_stale(self, handle: str) -> None:
        """Mark a window cache as stale without dropping it."""
        self._lock.lockForWrite()
        try:
            if handle in self._caches:
                self._caches[handle].is_stale = True
        finally:
            self._lock.unlock()

    def clear(self, handle: str) -> None:
        """Clear cache for a specific window handle."""
        self._lock.lockForWrite()
        try:
            self._caches.pop(handle, None)
            if self._active_handle == handle:
                self._active_handle = next(iter(self._caches.keys())) if self._caches else ""
        finally:
            self._lock.unlock()

    def clear_all(self) -> None:
        """Clear all cached window trees."""
        self._lock.lockForWrite()
        try:
            self._caches.clear()
            self._active_handle = ""
        finally:
            self._lock.unlock()

    def all_handles(self) -> List[str]:
        """Return all tracked window handles."""
        self._lock.lockForRead()
        try:
            return list(self._caches.keys())
        finally:
            self._lock.unlock()

    def merge_transient(self, handle: str, subtree: UINode) -> None:
        """
        Merge a UIA-captured transient subtree (e.g. F4 Freeze Snapshot)
        into the cached parsed tree and regenerate the live lxml tree.
        """
        self._lock.lockForWrite()
        try:
            target_handle = handle if handle and handle in self._caches else self._active_handle
            cache = self._caches.get(target_handle)
            if not cache:
                logger.warning("Cannot merge transient subtree: no cache for handle %s", target_handle)
                return

            # Merge into parsed UINode tree
            TreeParser.merge_subtree(cache.parsed_root, subtree)

            # Reconstruct live lxml tree and node_map from updated UINode hierarchy
            new_node_map: Dict[str, UINode] = {}
            cache.lxml_tree = TreeParser.to_xml_element(cache.parsed_root, new_node_map)
            cache.node_map = new_node_map
            cache.raw_xml = TreeParser.to_xml_string(cache.parsed_root)
            logger.info("Successfully merged transient subtree into cache for handle %s", target_handle)
        finally:
            self._lock.unlock()
