"""
Three-tier UI Tree Fetcher.
Streams Appium /source responses, handles partial scoped fetches, and reports progress.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, Optional
from lxml import etree
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot

from xgen.config import XGenConfig
from xgen.core.session_manager import SessionManager
from xgen.core.tree_cache import TreeCacheStore, WindowTreeCache
from xgen.core.tree_parser import TreeParser, UINode

logger = logging.getLogger("xgen.fetcher")


class FetchTier(Enum):
    UIA_DIRECT = 1    # In-process native UIA (instant hover/capture)
    SCOPED = 2        # GET /session/{id}/element/{eid}/source
    FULL = 3          # GET /session/{id}/source


class TreeFetchWorker(QObject):
    """Background worker executing the network fetch and XML parse."""
    started = pyqtSignal(int)               # FetchTier value
    progress = pyqtSignal(int, int)         # received, total
    finished = pyqtSignal(str, str, object) # handle, raw_xml, UINode root
    failed = pyqtSignal(str)                # error message
    large_tree_detected = pyqtSignal(int)   # node_count

    def __init__(self, session_manager: SessionManager, config: XGenConfig):
        super().__init__()
        self.session_manager = session_manager
        self.config = config
        self._is_cancelled = False

    @pyqtSlot(str, int)
    def do_fetch_full(self, window_handle: str, tier_val: int) -> None:
        self._is_cancelled = False
        self.started.emit(tier_val)

        try:
            # 1. Fetch raw XML from active Appium session
            raw_xml = self.session_manager.get_source(timeout_seconds=self.config.source_fetch_timeout_seconds)

            if self._is_cancelled:
                self.failed.emit("Tree fetch cancelled.")
                return

            # 2. Report progress
            total_bytes = len(raw_xml.encode("utf-8"))
            self.progress.emit(total_bytes, total_bytes)

            # 3. Check for large tree threshold
            node_estimate = raw_xml.count("<")
            if node_estimate >= self.config.large_tree_warning_threshold:
                self.large_tree_detected.emit(node_estimate)

            # 4. Parse into UINode in-memory hierarchy
            parsed_root = TreeParser.parse(raw_xml)
            if self._is_cancelled:
                self.failed.emit("Tree fetch cancelled.")
                return

            # 5. Compile live lxml ElementTree with direct UINode mapping for XPath querying
            node_map: Dict[str, UINode] = {}
            lxml_tree = TreeParser.to_xml_element(parsed_root, node_map)

            # 6. Update cache store
            cache = WindowTreeCache(
                window_handle=window_handle,
                raw_xml=raw_xml,
                parsed_root=parsed_root,
                lxml_tree=lxml_tree,
                node_map=node_map
            )
            TreeCacheStore.instance().set(window_handle, cache)

            self.finished.emit(window_handle, raw_xml, parsed_root)
        except Exception as e:
            if self._is_cancelled or not self.session_manager.session_id or "invalid session" in str(e).lower() or "terminated" in str(e).lower():
                logger.info("Tree fetch cancelled (session disconnected or terminated).")
                self.failed.emit("Tree fetch cancelled.")
            else:
                logger.error("Full tree fetch failed: %s", e)
                self.failed.emit(str(e))

    def cancel(self) -> None:
        self._is_cancelled = True


class TreeFetcher(QObject):
    """
    Public controller managing UI tree retrieval across windows and tiers.
    """
    fetch_started = pyqtSignal(int)            # FetchTier
    fetch_progress = pyqtSignal(int, int)      # bytes_recv, bytes_total
    fetch_complete = pyqtSignal(str, str, object)  # handle, raw_xml, UINode
    fetch_failed = pyqtSignal(str)
    large_tree_warning = pyqtSignal(int)       # node_count

    # Internal signal to worker
    _req_fetch_full = pyqtSignal(str, int)

    def __init__(self, session_manager: SessionManager, config: XGenConfig, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.config = config
        self._is_fetching = False
        # A user-initiated refresh that arrived mid-fetch, to be run as soon as
        # the in-flight one lands. See fetch_full() for why it isn't dropped.
        self._queued_handle: Optional[str] = None

        # Dedicated worker thread
        self._thread = QThread()
        self._worker = TreeFetchWorker(session_manager, config)
        self._worker.moveToThread(self._thread)

        # Wire request to worker slot
        self._req_fetch_full.connect(self._worker.do_fetch_full)

        # Connect internal worker signals
        self._worker.started.connect(self._on_started)
        self._worker.progress.connect(self.fetch_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.large_tree_detected.connect(self.large_tree_warning)

        self._thread.start()

    def close(self) -> None:
        """Shut down background fetch worker."""
        self._worker.cancel()
        self._thread.quit()
        self._thread.wait(2000)

    def fetch_full(self, window_handle: str = "", force: bool = False, queue_if_busy: bool = False) -> None:
        """
        Initiate asynchronous Tier 3 full tree fetch for the given window handle.

        `queue_if_busy` is for requests a *person* made (the Refresh button,
        Ctrl/Cmd+R). Those must never be silently discarded: dropping one looks
        exactly like a refresh that did nothing, which is why a second press
        appeared to be needed. The window for this is widest right after
        connecting, and wider still on macOS, where the first /source after
        attaching waits on WebDriverAgent warming up.
        """
        if self._is_fetching and not force:
            if queue_if_busy:
                self._queued_handle = window_handle
                logger.debug("Tree fetch in progress; queued the requested refresh to run after it.")
            else:
                logger.debug("Tree fetch already in progress, ignoring duplicate request.")
            return

        handle = window_handle
        if not handle:
            info = self.session_manager.session_info
            handle = info.active_handle if (info and info.active_handle) else "Root"

        self._is_fetching = True
        logger.info("Initiating full UI tree fetch for handle '%s'...", handle)
        self._req_fetch_full.emit(handle, FetchTier.FULL.value)

    def cancel(self) -> None:
        """Cancel ongoing fetch operation."""
        self._worker.cancel()
        self._is_fetching = False
        self._queued_handle = None

    # --- Worker Signal Handlers ---

    def _on_started(self, tier_val: int) -> None:
        self.fetch_started.emit(tier_val)

    def _on_finished(self, handle: str, raw_xml: str, root: UINode) -> None:
        self._is_fetching = False
        self.fetch_complete.emit(handle, raw_xml, root)
        self._run_queued_fetch()

    def _on_failed(self, err: str) -> None:
        self._is_fetching = False
        self.fetch_failed.emit(err)
        self._run_queued_fetch()

    def _run_queued_fetch(self) -> None:
        """Run a refresh that arrived while a fetch was already in flight."""
        if self._queued_handle is None:
            return
        handle = self._queued_handle
        self._queued_handle = None
        logger.info("Running the refresh that was requested during the previous fetch.")
        self.fetch_full(handle)
