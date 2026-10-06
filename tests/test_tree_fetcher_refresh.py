"""
Regression cover for refreshes being silently dropped.

TreeFetcher.fetch_full() returned early whenever a fetch was already running,
and the Refresh button called it without force — so pressing Refresh during the
post-connect fetch did nothing at all, and a second press was needed. The race
window is widest right after connecting and wider still on macOS, where the
first /source waits on WebDriverAgent warming up.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication

from xgen.config import XGenConfig
from xgen.core.tree_fetcher import TreeFetcher


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class _FakeSessionManager:
    session_id = "s1"
    session_info = None

    def get_source(self, timeout_seconds: int = 60) -> str:
        return "<AppiumAUT/>"


@pytest.fixture
def fetcher(qapp):
    f = TreeFetcher(_FakeSessionManager(), XGenConfig())
    # Keep the worker thread out of it: these tests are about the request
    # bookkeeping, not the network fetch.
    requests: list = []
    f._req_fetch_full.disconnect()
    f._req_fetch_full.connect(lambda handle, tier: requests.append(handle))
    f.requests = requests  # type: ignore[attr-defined]
    yield f
    f.close()


def test_a_user_refresh_during_a_fetch_is_not_dropped(fetcher):
    fetcher.fetch_full("Root", force=True)          # the post-connect fetch
    assert fetcher.requests == ["Root"]

    fetcher.fetch_full(queue_if_busy=True)          # user presses Refresh
    assert fetcher.requests == ["Root"], "should not fire while one is in flight"

    # ...and runs as soon as the in-flight fetch lands.
    fetcher._on_finished("Root", "<AppiumAUT/>", None)
    assert len(fetcher.requests) == 2


def test_a_queued_refresh_also_runs_after_a_failed_fetch(fetcher):
    fetcher.fetch_full("Root", force=True)
    fetcher.fetch_full(queue_if_busy=True)
    fetcher._on_failed("boom")
    assert len(fetcher.requests) == 2


def test_only_one_refresh_is_queued_no_matter_how_many_times_it_is_pressed(fetcher):
    fetcher.fetch_full("Root", force=True)
    for _ in range(5):
        fetcher.fetch_full(queue_if_busy=True)
    fetcher._on_finished("Root", "<AppiumAUT/>", None)
    assert len(fetcher.requests) == 2, "impatient clicking must not cause a fetch storm"


def test_background_callers_are_still_coalesced_away(fetcher):
    """Only requests a person made are queued; internal ones still collapse."""
    fetcher.fetch_full("Root", force=True)
    fetcher.fetch_full()                      # no queue_if_busy
    fetcher._on_finished("Root", "<AppiumAUT/>", None)
    assert len(fetcher.requests) == 1


def test_cancel_discards_a_queued_refresh(fetcher):
    fetcher.fetch_full("Root", force=True)
    fetcher.fetch_full(queue_if_busy=True)
    fetcher.cancel()
    fetcher._on_finished("Root", "<AppiumAUT/>", None)
    assert len(fetcher.requests) == 1, "a cancelled session must not fire a queued fetch"
