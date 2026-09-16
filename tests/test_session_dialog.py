"""
Unit tests for SessionDialog UI component with automatic window picker.
"""

import pytest
from PyQt6.QtWidgets import QApplication
from xgen.config import ConfigManager, RecentSession, XGenConfig
from xgen.core.session_manager import SessionManager
from xgen.ui.session_dialog import SessionDialog
from xgen.utils.window_finder import WindowTarget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_session_dialog_initialization_with_app_path(qapp):
    cfg = XGenConfig(appium_url="http://127.0.0.1:4723", app_path="C:\\app.exe")
    mgr = SessionManager()
    try:
        dlg = SessionDialog(cfg, mgr, auto_check=False)
        assert dlg.app_path_edit.text() == "C:\\app.exe"
        assert dlg.rb_launch_exe.isChecked() is True
        assert dlg.rb_window.isChecked() is False
        assert dlg.app_path_edit.isEnabled() is True
        assert dlg.combo_target.isEnabled() is False
        assert dlg.appium_url_edit.text() == "http://127.0.0.1:4723"

        # Switch to running window mode / Desktop Root
        dlg.rb_window.setChecked(True)
        assert dlg.rb_window.isChecked() is True
        assert dlg.rb_launch_exe.isChecked() is False
        assert dlg.combo_target.isEnabled() is True
        assert dlg.app_path_edit.isEnabled() is False

        dlg.combo_target.setCurrentIndex(0)
        assert isinstance(dlg.combo_target.currentData(), WindowTarget)
        assert dlg.combo_target.currentData().is_root is True
        dlg.close()
    finally:
        mgr.close()


def test_session_dialog_initialization_with_window_handle(qapp):
    cfg = XGenConfig(appium_url="http://127.0.0.1:4723", app_top_level_window="0xABC123")
    mgr = SessionManager()
    try:
        dlg = SessionDialog(cfg, mgr, auto_check=False)
        assert dlg.rb_window.isChecked() is True
        assert dlg.rb_launch_exe.isChecked() is False
        assert dlg.combo_target.isEnabled() is True
        assert dlg.app_path_edit.isEnabled() is False

        data = dlg.combo_target.currentData()
        assert isinstance(data, WindowTarget)
        assert data.handle_hex.lower() == "0xabc123"
        dlg.close()
    finally:
        mgr.close()


def test_session_dialog_connect_root(qapp):
    cfg = XGenConfig(appium_url="http://127.0.0.1:4723")
    mgr = SessionManager()
    try:
        dlg = SessionDialog(cfg, mgr, auto_check=False)
        dlg.rb_window.setChecked(True)
        dlg.combo_target.setCurrentIndex(0)

        emitted_configs = []
        dlg.session_requested.connect(lambda c: emitted_configs.append(c))

        dlg._on_connect_clicked()
        assert len(emitted_configs) == 1
        assert emitted_configs[0].app_path == ""
        assert emitted_configs[0].app_top_level_window == ""
        dlg.close()
    finally:
        mgr.close()


def test_session_dialog_connect_launch_exe(qapp):
    cfg = XGenConfig(appium_url="http://127.0.0.1:4723")
    mgr = SessionManager()
    try:
        dlg = SessionDialog(cfg, mgr, auto_check=False)
        dlg.rb_launch_exe.setChecked(True)
        dlg.app_path_edit.setText("C:\\Program Files\\TestApp\\test.exe")

        emitted_configs = []
        dlg.session_requested.connect(lambda c: emitted_configs.append(c))

        dlg._on_connect_clicked()
        assert len(emitted_configs) == 1
        assert emitted_configs[0].app_path == "C:\\Program Files\\TestApp\\test.exe"
        assert emitted_configs[0].app_top_level_window == ""
        dlg.close()
    finally:
        mgr.close()


def test_session_dialog_connect_empty_exe_validation(qapp):
    cfg = XGenConfig(appium_url="http://127.0.0.1:4723")
    mgr = SessionManager()
    try:
        dlg = SessionDialog(cfg, mgr, auto_check=False)
        dlg.rb_launch_exe.setChecked(True)
        dlg.app_path_edit.setText("")

        emitted_configs = []
        dlg.session_requested.connect(lambda c: emitted_configs.append(c))

        dlg._on_connect_clicked()
        assert len(emitted_configs) == 0
        assert "Please specify an application executable path" in dlg.lbl_status.text()
        dlg.close()
    finally:
        mgr.close()

