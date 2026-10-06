import pytest
from PyQt6.QtWidgets import QApplication
from xgen.core.tree_parser import TreeParser

SAMPLE_XML = """<AppiumAUT>
  <Window Name='Main Window' AutomationId='win_main' ClassName='MainWnd'>
    <Pane Name='Container' AutomationId='pan_container'>
      <Button Name='Submit' AutomationId='btn_submit' ClassName='ButtonClass'/>
      <Button Name='Duplicate' ClassName='ButtonClass'/>
      <Button Name='Duplicate' ClassName='ButtonClass'/>
      <Edit Name='Username' AutomationId='1001' ClassName='EditClass'/>
      <Button Name='He said "Hello"' ClassName='ButtonClass'/>
    </Pane>
  </Window>
</AppiumAUT>"""

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def pin_windows_driver(monkeypatch):
    """Keep the suite's *default* driver vocabulary independent of the host OS.

    Which driver xGen assumes is deliberately not sys.platform — it comes from
    the session's capabilities, falling back to the local platform backend, so
    that xGen on Windows can drive a remote Mac Appium server. That is right
    for the product and a trap for the tests: most of this suite asserts
    Windows behaviour against Windows-shaped input (SAMPLE_XML's Name= and
    AutomationId= attributes, Desktop Root targets, appTopLevelWindow
    capabilities, "application executable path" wording). Run on a macOS
    runner, all of that silently resolved to Mac2 vocabulary instead —
    @label/@title, application targets, bundle-ID wording — and seven tests
    failed for a reason that had nothing to do with what they were checking.

    So the default is pinned here rather than inherited from whichever machine
    happens to be running. Tests that are about another driver still override
    it the normal way: an explicit config.target_platform, or activating a
    dialect themselves, both take precedence over this.
    """
    from xgen.core.driver_dialect import WINDOWS_DIALECT, DriverDialectStore
    from xgen.platform.factory import get_platform_backend

    DriverDialectStore.instance().set_active(WINDOWS_DIALECT)
    try:
        backend = get_platform_backend()
        monkeypatch.setattr(
            type(backend), "default_driver_platform", lambda self: "windows", raising=False
        )
    except Exception:
        pass  # best effort; a test that cares pins its own platform anyway
    yield
    DriverDialectStore.instance().reset()

@pytest.fixture
def parsed_data():
    from lxml import etree
    root = TreeParser.parse(SAMPLE_XML)
    lxml_tree = etree.fromstring(SAMPLE_XML.encode())
    return root, lxml_tree

@pytest.fixture
def test_dir():
    """Isolated temporary directory inside workspace for test executions."""
    import os
    import time
    from pathlib import Path
    import shutil

    scratch_base = Path(__file__).resolve().parent.parent / ".pytest_scratch"
    d = scratch_base / f"t_{os.getpid()}_{int(time.time() * 1000)}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
