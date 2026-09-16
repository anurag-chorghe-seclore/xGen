"""
Unit tests for UI Panels (Toolbar, TreePanel, AttributePanel, XPathPanel, MainWindow).
"""

import pytest
from lxml import etree
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from xgen.config import XGenConfig
from xgen.core.tree_parser import TreeParser, UINode
from xgen.ui.attribute_panel import AttributePanel
from xgen.ui.main_window import MainWindow
from xgen.ui.toolbar import Toolbar
from xgen.ui.tree_panel import TreePanel
from xgen.ui.xpath_panel import XPathPanel

SAMPLE_XML = """
<AppiumAUT>
  <Window Name="Test App" AutomationId="win_test">
    <Button Name="Submit" AutomationId="btn_submit" ClassName="WPFButton"/>
  </Window>
</AppiumAUT>
"""


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_toolbar_state_changes(qapp):
    toolbar = Toolbar()
    assert "Disconnected" in toolbar.btn_status_dot.toolTip()

    toolbar.set_session_state("connected", "Notepad.exe")
    assert "Connected to: Notepad.exe" in toolbar.btn_status_dot.toolTip()


def test_toolbar_timed_capture_button_states(qapp):
    toolbar = Toolbar()
    assert toolbar.btn_timed.text() == "⏱ 5s"
    assert toolbar.btn_timed.isEnabled() is True

    toolbar.set_timed_countdown(4)
    assert "4s" in toolbar.btn_timed.text()
    assert toolbar.btn_timed.isEnabled() is False

    toolbar.set_timed_countdown(0)
    assert "Capturing" in toolbar.btn_timed.text()
    assert toolbar.btn_timed.isEnabled() is False

    toolbar.reset_timed_button()
    assert toolbar.btn_timed.text() == "⏱ 5s"
    assert toolbar.btn_timed.isEnabled() is True


def test_toolbar_refresh_button_states(qapp):
    toolbar = Toolbar()
    assert toolbar.btn_refresh.text() == "🔄 Refresh"
    assert toolbar.btn_refresh.isEnabled() is True

    toolbar.set_refreshing(True)
    assert toolbar.btn_refresh.text() == "🔄 Refreshing..."
    assert toolbar.btn_refresh.isEnabled() is False

    toolbar.set_refreshing(False)
    assert toolbar.btn_refresh.text() == "🔄 Refresh"
    assert toolbar.btn_refresh.isEnabled() is True



def test_toolbar_update_windows_list_handle_normalization(qapp):
    from xgen.utils.window_finder import WindowTarget

    toolbar = Toolbar()
    windows = [
        WindowTarget(title="Desktop Root (Entire OS)", handle_hex="", hwnd=0, exe_name="", is_root=True),
        WindowTarget(title="Calculator", handle_hex="0x000E07FA", hwnd=919546, exe_name="calc.exe"),
        WindowTarget(title="Notepad", handle_hex="0x00020B1A", hwnd=133914, exe_name="notepad.exe"),
    ]

    # Select Calculator via padded hex
    toolbar.update_windows_list(windows, selected_handle="0x000E07FA")
    assert toolbar.combo_windows.currentIndex() == 1
    assert "Calculator" in toolbar.combo_windows.currentText()

    # Update with unpadded hex - should still match Calculator and NOT reset to Root
    toolbar.update_windows_list(windows, selected_handle="0xe07fa")
    assert toolbar.combo_windows.currentIndex() == 1
    assert "Calculator" in toolbar.combo_windows.currentText()

    # Update with decimal handle - should still match Calculator and NOT reset to Root
    toolbar.update_windows_list(windows, selected_handle="919546")
    assert toolbar.combo_windows.currentIndex() == 1
    assert "Calculator" in toolbar.combo_windows.currentText()

    # Update with None / empty - should PRESERVE current user selection (Calculator)
    toolbar.update_windows_list(windows, selected_handle="")
    assert toolbar.combo_windows.currentIndex() == 1
    assert "Calculator" in toolbar.combo_windows.currentText()


def test_tree_panel_and_attribute_panel_population(qapp):
    root = TreeParser.parse(SAMPLE_XML)
    btn_node = root.children[0].children[0]

    tree_panel = TreePanel()
    tree_panel.populate(root)
    assert tree_panel.tree_widget.topLevelItemCount() == 1

    attr_panel = AttributePanel()
    attr_panel.populate(btn_node)
    assert attr_panel.table.rowCount() >= 3

    # Test collapsible toggle
    assert attr_panel.table.isHidden() is False
    attr_panel.toggle_collapse()
    assert attr_panel.table.isHidden() is True
    assert "Collapsed" in attr_panel.lbl_title.text()
    attr_panel.toggle_collapse()
    assert attr_panel.table.isHidden() is False


def test_tree_deep_search_filtering(qapp):
    root = TreeParser.parse(SAMPLE_XML)
    tree_panel = TreePanel()
    tree_panel.populate(root)

    # Search for node "Submit"
    tree_panel.search_edit.setText("Submit")
    tree_panel._perform_search()
    # Matching item must be instantiated and not hidden
    matching_item = None
    for item in tree_panel._node_item_map.values():
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if node and node.name == "Submit":
            matching_item = item
            break

    assert matching_item is not None
    assert matching_item.isHidden() is False

    # Clear search
    tree_panel.search_edit.clear()
    tree_panel._perform_search()
    assert matching_item.isHidden() is False


def test_xpath_panel_generation_and_prefix_toggle(qapp):
    root = TreeParser.parse(SAMPLE_XML)
    lxml_tree = etree.fromstring(SAMPLE_XML.encode())
    btn_node = root.children[0].children[0]

    xpath_panel = XPathPanel()
    xpath_panel.populate(btn_node, tree_root=root, lxml_tree=lxml_tree)
    assert len(xpath_panel._candidates) >= 1
    assert "Button" in xpath_panel._candidates[0].xpath
    assert "Submit" in xpath_panel._candidates[0].xpath

    # Toggle container prefix
    xpath_panel.chk_prefix.setChecked(True)
    assert xpath_panel.chk_prefix.isChecked() is True
    assert xpath_panel._candidates[0].xpath.startswith("//Window")


def test_main_window_assembly(qapp):
    cfg = XGenConfig(auto_connect_on_startup=False)
    win = MainWindow(cfg, start_hooks=False)
    assert win.tree_panel is not None
    assert win.attr_panel is not None
    assert win.xpath_panel is not None
    assert win.toolbar is not None
    assert win.status_bar is not None
    assert win.toolbar.combo_windows.count() >= 1
    assert "Desktop Root" in win.toolbar.combo_windows.itemText(0)

    # Test selecting target when disconnected
    win._on_window_switched("")
    assert "Target selected: Desktop Root" in win.status_bar.lbl_msg.text()

    # Test tree fetch complete handler
    root = TreeParser.parse(SAMPLE_XML)
    win._on_tree_fetch_complete("0x0001", SAMPLE_XML, root)
    assert "Tree ready" in win.status_bar.lbl_msg.text()

    # Test clicking [Select in Tree] on a node from XPathPanel drawer
    btn_node = root.children[0].children[0]
    win._on_xpath_node_selected(btn_node)
    assert win.attr_panel.table.rowCount() > 0

    win.session_manager.close()
    win.tree_fetcher.close()
    win.close()


def test_legend_dialog_initialization(qapp):
    from xgen.ui.legend_dialog import LegendDialog
    dlg = LegendDialog()
    assert dlg.windowTitle() == "xGen — XPath Guide & Index"
    dlg.close()


def test_xpath_card_test_and_click_signals(qapp):
    root = TreeParser.parse(SAMPLE_XML)
    lxml_tree = etree.fromstring(SAMPLE_XML.encode())
    btn_node = root.children[0].children[0]

    xpath_panel = XPathPanel()
    xpath_panel.populate(btn_node, tree_root=root, lxml_tree=lxml_tree)
    assert len(xpath_panel._cards) >= 1

    card = xpath_panel._cards[0]
    test_signals = []
    click_signals = []
    hover_signals = []
    type_signals = []

    xpath_panel.test_requested.connect(lambda xp, c: test_signals.append((xp, c)))
    xpath_panel.click_requested.connect(lambda xp, c: click_signals.append((xp, c)))
    xpath_panel.hover_requested.connect(lambda xp, c: hover_signals.append((xp, c)))
    xpath_panel.type_requested.connect(lambda xp, t, c: type_signals.append((xp, t, c)))

    # Test clicking Test button
    card.btn_test.click()
    assert len(test_signals) == 1
    assert test_signals[0][0] == card.candidate.xpath
    assert test_signals[0][1] == card

    # Test clicking Click button after showing test result
    card.show_test_result(True, 12.0)
    assert card.btn_click.isHidden() is False
    assert card.btn_hover.isHidden() is False
    assert card.btn_type.isHidden() is False

    card.btn_click.click()
    assert len(click_signals) == 1
    assert click_signals[0][0] == card.candidate.xpath
    assert click_signals[0][1] == card

    card.btn_hover.click()
    assert len(hover_signals) == 1
    assert hover_signals[0][0] == card.candidate.xpath
    assert hover_signals[0][1] == card

    # Test inline typing
    card.btn_type.click()
    assert card.type_bar.isHidden() is False
    card.input_type_text.setText("Hello World")
    card.btn_submit_type.click()
    assert len(type_signals) == 1
    assert type_signals[0][0] == card.candidate.xpath
    assert type_signals[0][1] == "Hello World"
    assert type_signals[0][2] == card


def test_session_dialog_confirm_disconnect_setting(qapp):
    from xgen.ui.session_dialog import SessionDialog
    from xgen.core.session_manager import SessionManager

    sm = SessionManager()
    try:
        cfg = XGenConfig(confirm_disconnect=True)
        dlg = SessionDialog(cfg, sm, auto_check=False)
        assert dlg.chk_confirm_disconnect.isChecked() is True

        dlg.chk_confirm_disconnect.setChecked(False)
        assert dlg.config.confirm_disconnect is False
        dlg.close()
    finally:
        sm.close()


def test_session_dialog_confirm_reconnect_switch_setting(qapp):
    from xgen.ui.session_dialog import SessionDialog
    from xgen.core.session_manager import SessionManager

    sm = SessionManager()
    try:
        cfg = XGenConfig(confirm_reconnect_switch=True)
        dlg = SessionDialog(cfg, sm, auto_check=False)
        assert dlg.chk_confirm_reconnect_switch.isChecked() is True

        dlg.chk_confirm_reconnect_switch.setChecked(False)
        assert dlg.config.confirm_reconnect_switch is False
        dlg.close()
    finally:
        sm.close()


def test_main_window_pin_toggle(qapp):
    import platform
    import ctypes
    from ctypes import wintypes

    WS_EX_TOPMOST = 0x00000008
    GWL_EXSTYLE = -20

    def is_topmost(hwnd: int) -> bool:
        if platform.system() != "Windows":
            return False
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        return bool(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE) & WS_EX_TOPMOST)

    # 1. Test runtime toggle
    cfg = XGenConfig(auto_connect_on_startup=False, pin_on_top=False)
    win = MainWindow(cfg, start_hooks=False)
    win.show()
    QApplication.processEvents()

    if platform.system() == "Windows":
        assert not is_topmost(int(win.winId())), "Should not be topmost initially"
    win.toolbar.btn_pin.click()  # Pin ON
    QApplication.processEvents()
    if platform.system() == "Windows":
        assert is_topmost(int(win.winId())), "Should be topmost after clicking Pin"
    win.toolbar.btn_pin.click()  # Pin OFF
    QApplication.processEvents()
    if platform.system() == "Windows":
        assert not is_topmost(int(win.winId())), "Should not be topmost after clicking Unpin"

    win.session_manager.close()
    win.tree_fetcher.close()
    win.close()

    # 2. Test startup restore path
    cfg2 = XGenConfig(auto_connect_on_startup=False, pin_on_top=True)
    win2 = MainWindow(cfg2, start_hooks=False)
    win2.show()
    QApplication.processEvents()  # Let singleShot(0) fire
    if platform.system() == "Windows":
        assert is_topmost(int(win2.winId())), "Should be topmost when restored from config"

    win2.toolbar.btn_pin.setChecked(False)  # Unpin cleanly before closing
    QApplication.processEvents()
    win2.session_manager.close()
    win2.tree_fetcher.close()
    win2.close()


def test_export_xml_flow(qapp):
    from xgen.core.tree_cache import TreeCacheStore
    TreeCacheStore.instance().clear_all()

    cfg = XGenConfig(auto_connect_on_startup=False)
    win = MainWindow(cfg, start_hooks=False)
    try:
        assert hasattr(win.toolbar, "btn_export_xml")
        assert hasattr(win.toolbar, "btn_overflow")
        assert not hasattr(win.toolbar, "btn_freeze")

        # When no cache is active, status bar warns
        win._on_export_xml()
        assert "No page source available" in win.status_bar.lbl_msg.text()

        # Populate tree cache
        root = TreeParser.parse(SAMPLE_XML)
        win._on_tree_fetch_complete("0x0001", SAMPLE_XML, root)

        # Call export XML
        win._on_export_xml()
        assert "Exported page source to" in win.status_bar.lbl_msg.text()
    finally:
        win.session_manager.close()
        win.tree_fetcher.close()
        win.close()
        TreeCacheStore.instance().clear_all()


def test_main_window_window_switch_cancel_and_allow(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from xgen.core.session_manager import SessionInfo, SessionState
    from unittest.mock import MagicMock

    monkeypatch.setattr("xgen.config.ConfigManager.save", lambda cfg, path=None: None)
    cfg = XGenConfig(auto_connect_on_startup=False, confirm_reconnect_switch=True)
    win = MainWindow(cfg, start_hooks=False)
    try:
        # Mock active session
        win.session_manager.state = SessionState.CONNECTED
        win.session_manager._session_id = "sid-123"
        win.session_manager.session_info = SessionInfo(
            session_id="sid-123",
            appium_url="http://127.0.0.1:4723",
            app_name="App 1",
            windows=[],
            active_handle="0x1111"
        )
        win.config.app_top_level_window = "0x1111"

        # Populate combo
        win.toolbar.combo_windows.blockSignals(True)
        win.toolbar.combo_windows.clear()
        win.toolbar.combo_windows.addItem("Desktop Root", "")
        win.toolbar.combo_windows.addItem("App 1 [0x1111]", "0x1111")
        win.toolbar.combo_windows.addItem("App 2 [0x2222]", "0x2222")
        win.toolbar.combo_windows.setCurrentIndex(1)
        win.toolbar.combo_windows.blockSignals(False)

        reconnect_mock = MagicMock()
        monkeypatch.setattr(win.session_manager, "reconnect", reconnect_mock)

        # 1. Test Cancel: should revert to index 1 (App 1) and NOT call reconnect
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel)

        win._on_window_switched("0x2222")
        assert reconnect_mock.call_count == 0
        assert win.toolbar.combo_windows.currentIndex() == 1
        assert win.toolbar.combo_windows.currentData() == "0x1111"

        # 2. Test Allow with remember choice
        def mock_exec_allow(self):
            # simulate checking remember checkbox
            cb = self.checkBox()
            if cb:
                cb.setChecked(True)
            return QMessageBox.StandardButton.Ok

        monkeypatch.setattr(QMessageBox, "exec", mock_exec_allow)

        win._on_window_switched("0x2222")
        assert reconnect_mock.call_count == 1
        assert win.config.app_top_level_window == "0x2222"
        assert win.config.confirm_reconnect_switch is False

        # 3. Test subsequent switch with confirm_reconnect_switch == False: no prompt, instant reconnect
        reconnect_mock.reset_mock()
        win._on_window_switched("")
        assert reconnect_mock.call_count == 1
        assert win.config.app_top_level_window == ""
    finally:
        win.session_manager.close()
        win.tree_fetcher.close()
        win.close()



