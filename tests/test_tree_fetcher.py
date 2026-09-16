"""
Unit tests for TreeFetcher.
"""

import pytest
import responses
from PyQt6.QtCore import QCoreApplication
from xgen.config import XGenConfig
from xgen.core.session_manager import SessionManager
from xgen.core.tree_fetcher import TreeFetcher, FetchTier


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@responses.activate
def test_tree_fetcher_full_cycle(qapp):
    mgr = SessionManager()
    mgr._session_id = "test-sid-999"
    mgr._base_url = "http://127.0.0.1:4723"

    sample_xml = "<AppiumAUT><Window Name='Test'><Button Name='OK'/></Window></AppiumAUT>"
    responses.add(
        responses.GET,
        "http://127.0.0.1:4723/session/test-sid-999/source",
        json={"value": sample_xml},
        status=200
    )

    cfg = XGenConfig(appium_url="http://127.0.0.1:4723")
    fetcher = TreeFetcher(mgr, cfg)

    # Worker direct invocation to test complete parsing & cache pipeline
    worker = fetcher._worker
    worker.do_fetch_full("0x0001", FetchTier.FULL.value)

    from xgen.core.tree_cache import TreeCacheStore
    cache = TreeCacheStore.instance().get("0x0001")
    assert cache is not None
    assert cache.parsed_root.children[0].name == "Test"
    assert cache.parsed_root.children[0].children[0].name == "OK"

    fetcher.close()
    mgr.close()

