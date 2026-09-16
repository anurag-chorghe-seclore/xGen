"""
Session Setup and Connection Dialog.
Allows user to launch apps, attach to windows, or inspect Desktop root.
"""

import datetime
from pathlib import Path
from typing import Optional
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from xgen.config import ConfigManager, RecentSession, XGenConfig
from xgen.core.session_manager import SessionManager
from xgen.utils.window_finder import WindowTarget, get_open_windows, normalize_handle


class PingWorker(QObject):
    """Background worker to check Appium reachability without UI blocking or flicker."""
    finished = pyqtSignal(bool, str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._is_cancelled = False

    def cancel(self) -> None:
        self._is_cancelled = True

    def run(self) -> None:
        is_running, msg = SessionManager.check_server_status(self.url, timeout_seconds=1.5)
        if not self._is_cancelled:
            try:
                self.finished.emit(is_running, msg)
            except RuntimeError:
                pass


class SessionDialog(QDialog):
    """Modal dialog to configure and initiate an Appium Windows Driver session."""
    session_requested = pyqtSignal(object)  # XGenConfig

    def __init__(self, config: XGenConfig, session_manager: SessionManager, parent: Optional[QWidget] = None, auto_check: bool = True):
        super().__init__(parent)
        self.config = config
        self.session_manager = session_manager
        self._ping_thread: Optional[QThread] = None
        self._ping_worker: Optional[PingWorker] = None
        self.setWindowTitle("xGen — Connect Session")
        self.setMinimumWidth(540)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background: #0f1115; color: #f1f5f9; font-family: 'Segoe UI', system-ui, sans-serif; }
            QFrame#card { background-color: #14171e; border: 1px solid #232834; border-radius: 8px; }
            QLabel#section_title { color: #60a5fa; font-size: 10px; font-weight: 700; letter-spacing: 0.6px; margin-top: 4px; }
            QRadioButton { color: #cbd5e1; font-size: 11px; spacing: 8px; padding: 3px 0; font-weight: 500; }
            QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #475569; background: #181c24; }
            QRadioButton::indicator:hover { border-color: #3b82f6; }
            QRadioButton::indicator:checked { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #3b82f6; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 #3b82f6, stop:0.55 #3b82f6, stop:0.6 #181c24, stop:1.0 #181c24); }
            QRadioButton:hover { color: #ffffff; }
            QLineEdit { background: #181c24; color: #f1f5f9; border: 1px solid #2a3140; border-radius: 6px; padding: 6px 10px; font-size: 11px; }
            QLineEdit:focus { border-color: #3b82f6; }
            QLineEdit:disabled { background: #111317; color: #475569; border-color: #1e222b; }
            QLabel { color: #cbd5e1; font-size: 11px; }
            QLabel:disabled { color: #475569; }
            QComboBox { background: #181c24; color: #f1f5f9; border: 1px solid #2a3140; border-radius: 6px; padding: 6px 10px; font-size: 11px; }
            QComboBox:hover { border-color: #3b82f6; }
            QComboBox:disabled { background: #111317; color: #475569; border-color: #1e222b; }
            QComboBox QAbstractItemView { background: #14171e; color: #cbd5e1; selection-background-color: #2563eb; selection-color: #ffffff; border: 1px solid #28303f; border-radius: 6px; padding: 4px; outline: none; }
            QComboBox QAbstractItemView::item { min-height: 24px; padding: 4px 10px; border-radius: 4px; border-bottom: 1px solid #1e2430; margin: 1px 0px; }
            QComboBox QAbstractItemView::item:hover { background-color: #1e2533; color: #ffffff; }
            QComboBox QAbstractItemView::item:selected { background-color: #2563eb; color: #ffffff; font-weight: 500; }
            QPushButton { background: #1e2430; color: #cbd5e1; border: 1px solid #2e384d; border-radius: 6px; padding: 6px 14px; font-size: 11px; font-weight: 500; }
            QPushButton:hover { background: #2b3548; color: #ffffff; border-color: #3b82f6; }
            QPushButton:disabled { background: #111317; color: #475569; border-color: #1e222b; }
        """)

        self._init_ui()
        self._populate_from_config(config)
        if auto_check:
            self.check_server_status()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # Radio button group for target mode
        self.target_group = QButtonGroup(self)

        # 1. Target Window / Application Picker Card
        lbl_target_hdr = QLabel("RUNNING WINDOW TARGET")
        lbl_target_hdr.setObjectName("section_title")
        main_layout.addWidget(lbl_target_hdr)

        target_card = QFrame()
        target_card.setObjectName("card")
        target_card_layout = QVBoxLayout(target_card)
        target_card_layout.setContentsMargins(12, 10, 12, 10)
        target_card_layout.setSpacing(6)

        self.rb_window = QRadioButton("Attach to Running Window or Desktop Root")
        self.rb_window.setChecked(True)
        self.target_group.addButton(self.rb_window)
        target_card_layout.addWidget(self.rb_window)

        combo_row = QHBoxLayout()
        combo_row.setSpacing(6)

        self.combo_target = QComboBox()
        self.combo_target.setToolTip("Select running application window or Desktop Root")
        if self.combo_target.view() and self.combo_target.view().window():
            self.combo_target.view().window().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.combo_target.view().window().setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        self.combo_target.currentIndexChanged.connect(self._on_target_changed)
        combo_row.addWidget(self.combo_target, 1)

        self.btn_refresh_targets = QPushButton("🔄 Refresh")
        self.btn_refresh_targets.setToolTip("Scan system for newly opened windows")
        self.btn_refresh_targets.clicked.connect(self._refresh_window_combo)
        combo_row.addWidget(self.btn_refresh_targets)

        target_card_layout.addLayout(combo_row)

        self.lbl_target_badge = QLabel("Target: Desktop Root (Entire OS)")
        self.lbl_target_badge.setStyleSheet("color: #60a5fa; font-size: 10px; font-family: monospace;")
        target_card_layout.addWidget(self.lbl_target_badge)

        main_layout.addWidget(target_card)

        # 2. Launch Application Executable (.exe) Card (Replaces Recent Configurations)
        lbl_launch_hdr = QLabel("LAUNCH APPLICATION EXECUTABLE (.EXE)")
        lbl_launch_hdr.setObjectName("section_title")
        main_layout.addWidget(lbl_launch_hdr)

        launch_card = QFrame()
        launch_card.setObjectName("card")
        launch_card_layout = QVBoxLayout(launch_card)
        launch_card_layout.setContentsMargins(12, 10, 12, 10)
        launch_card_layout.setSpacing(6)

        self.rb_launch_exe = QRadioButton("Launch New Application Process from Executable (.exe)")
        self.target_group.addButton(self.rb_launch_exe)
        launch_card_layout.addWidget(self.rb_launch_exe)

        self.app_path_row_widget = QWidget()
        app_path_row = QHBoxLayout(self.app_path_row_widget)
        app_path_row.setContentsMargins(0, 0, 0, 0)
        app_path_row.setSpacing(6)

        self.app_path_edit = QLineEdit()
        self.app_path_edit.setPlaceholderText("C:\\Program Files\\...\\app.exe")
        self.app_path_edit.textChanged.connect(self._on_app_path_text_changed)

        self.btn_browse = QPushButton("📁 Browse...")
        self.btn_browse.clicked.connect(self._browse_app_path)

        app_path_row.addWidget(self.app_path_edit, 1)
        app_path_row.addWidget(self.btn_browse)
        launch_card_layout.addWidget(self.app_path_row_widget)

        lbl_exe_hint = QLabel("Starts a new application process from disk and connects inspection to its main window.")
        lbl_exe_hint.setStyleSheet("color: #64748b; font-size: 10px;")
        launch_card_layout.addWidget(lbl_exe_hint)

        main_layout.addWidget(launch_card)

        # Wire mutual mode toggle
        self.rb_window.toggled.connect(self._on_mode_toggled)

        # 3. Server Configuration Card
        lbl_server_hdr = QLabel("APPIUM SERVER CONFIGURATION")
        lbl_server_hdr.setObjectName("section_title")
        main_layout.addWidget(lbl_server_hdr)

        server_card = QFrame()
        server_card.setObjectName("card")
        server_layout = QFormLayout(server_card)
        server_layout.setContentsMargins(12, 10, 12, 10)
        server_layout.setSpacing(8)
        server_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.appium_url_edit = QLineEdit("http://127.0.0.1:4723")
        self.btn_test_url = QPushButton("Ping Status")
        self.btn_test_url.setFixedWidth(100)
        self.btn_test_url.clicked.connect(self.check_server_status)
        url_row = QHBoxLayout()
        url_row.addWidget(self.appium_url_edit)
        url_row.addWidget(self.btn_test_url)
        server_layout.addRow("Appium URL:", url_row)

        main_layout.addWidget(server_card)

        # 4. Options / Preferences
        self.chk_confirm_disconnect = QCheckBox("Ask confirmation before disconnecting active session")
        self.chk_confirm_disconnect.setToolTip("Show a confirmation prompt when clicking the connected status dot to disconnect")
        self.chk_confirm_disconnect.setStyleSheet("""
            QCheckBox { color: #cbd5e1; font-size: 11px; padding: 2px 0; }
            QCheckBox:hover { color: #ffffff; }
            QCheckBox::indicator { width: 14px; height: 14px; border-radius: 3px; border: 1px solid #334155; background: #181c24; }
            QCheckBox::indicator:checked { background: #2563eb; border-color: #3b82f6; }
        """)
        self.chk_confirm_disconnect.toggled.connect(self._on_confirm_disconnect_toggled)
        main_layout.addWidget(self.chk_confirm_disconnect)

        self.chk_confirm_reconnect_switch = QCheckBox("Ask confirmation before reconnecting when switching windows")
        self.chk_confirm_reconnect_switch.setToolTip("Show a confirmation prompt when switching target window or app while connected")
        self.chk_confirm_reconnect_switch.setStyleSheet("""
            QCheckBox { color: #cbd5e1; font-size: 11px; padding: 2px 0; }
            QCheckBox:hover { color: #ffffff; }
            QCheckBox::indicator { width: 14px; height: 14px; border-radius: 3px; border: 1px solid #334155; background: #181c24; }
            QCheckBox::indicator:checked { background: #2563eb; border-color: #3b82f6; }
        """)
        self.chk_confirm_reconnect_switch.toggled.connect(self._on_confirm_reconnect_switch_toggled)
        main_layout.addWidget(self.chk_confirm_reconnect_switch)

        # 5. Status Indicator Banner
        self.lbl_status = QLabel("Checking Appium server status...")
        self.lbl_status.setStyleSheet("background: #1e2433; color: #94a3b8; border: 1px solid #2e384d; border-radius: 6px; padding: 6px 10px; font-size: 11px;")
        main_layout.addWidget(self.lbl_status)

        # 6. Dialog Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setStyleSheet("QPushButton { background: #181c24; color: #94a3b8; border: 1px solid #2a3140; border-radius: 6px; padding: 7px 18px; font-size: 11px; font-weight: 500; } QPushButton:hover { background: #222834; color: #f1f5f9; }")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setDefault(True)
        self.btn_connect.setStyleSheet("QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #3b82f6); color: #ffffff; border: 1px solid #60a5fa; border-radius: 6px; padding: 7px 22px; font-size: 11px; font-weight: bold; } QPushButton:hover { background: #1d4ed8; }")
        self.btn_connect.clicked.connect(self._on_connect_clicked)

        button_row.addWidget(self.btn_cancel)
        button_row.addWidget(self.btn_connect)
        main_layout.addLayout(button_row)

    def _on_mode_toggled(self, is_window: bool) -> None:
        self.combo_target.setEnabled(is_window)
        self.btn_refresh_targets.setEnabled(is_window)
        self.app_path_edit.setEnabled(not is_window)
        self.btn_browse.setEnabled(not is_window)
        if is_window:
            self._on_target_changed(self.combo_target.currentIndex())
        else:
            self.lbl_target_badge.setText("Target: Launch new process from executable path")
            self.lbl_target_badge.show()

    def _on_app_path_text_changed(self, text: str) -> None:
        if text.strip() and not self.rb_launch_exe.isChecked():
            self.rb_launch_exe.setChecked(True)

    def _refresh_window_combo(self) -> None:
        """Scan top-level windows and repopulate combo, preserving current selection if possible."""
        prev_data = self.combo_target.currentData()
        prev_handle = prev_data.handle_hex if isinstance(prev_data, WindowTarget) else ""

        self.combo_target.blockSignals(True)
        self.combo_target.clear()

        windows = get_open_windows()
        selected_idx = 0
        norm_prev = normalize_handle(prev_handle)
        for idx, w in enumerate(windows):
            self.combo_target.addItem(w.display_label(), w)
            if norm_prev is not None and normalize_handle(w.handle_hex) == norm_prev:
                selected_idx = idx

        self.combo_target.setCurrentIndex(selected_idx)
        self.combo_target.blockSignals(False)
        self._on_target_changed(self.combo_target.currentIndex())

    def _on_target_changed(self, index: int) -> None:
        if index < 0:
            return
        if not self.rb_window.isChecked():
            self.rb_window.setChecked(True)
        data = self.combo_target.itemData(index)
        if isinstance(data, WindowTarget):
            if data.is_root:
                self.lbl_target_badge.setText("Target: Desktop Root (Entire OS)")
                self.lbl_target_badge.show()
            else:
                exe_part = f"  |  Exe: {data.exe_name}" if data.exe_name else ""
                self.lbl_target_badge.setText(f"HWND Handle: {data.handle_hex}{exe_part}")
                self.lbl_target_badge.show()
        else:
            self.lbl_target_badge.hide()

    def _on_confirm_disconnect_toggled(self, checked: bool) -> None:
        self.config.confirm_disconnect = checked
        ConfigManager.save(self.config)

    def _on_confirm_reconnect_switch_toggled(self, checked: bool) -> None:
        self.config.confirm_reconnect_switch = checked
        ConfigManager.save(self.config)

    def _populate_from_config(self, cfg: XGenConfig) -> None:
        self.appium_url_edit.setText(cfg.appium_url or "http://127.0.0.1:4723")
        self.app_path_edit.setText(cfg.app_path or "")
        self.chk_confirm_disconnect.setChecked(getattr(cfg, "confirm_disconnect", True))
        self.chk_confirm_reconnect_switch.setChecked(getattr(cfg, "confirm_reconnect_switch", True))

        self._refresh_window_combo()

        if cfg.app_path:
            self.rb_launch_exe.setChecked(True)
        elif cfg.app_top_level_window:
            self.rb_window.setChecked(True)
            matched_idx = -1
            norm_target = normalize_handle(cfg.app_top_level_window)
            for i in range(self.combo_target.count()):
                d = self.combo_target.itemData(i)
                if isinstance(d, WindowTarget) and normalize_handle(d.handle_hex) == norm_target:
                    matched_idx = i
                    break
            if matched_idx >= 0:
                self.combo_target.setCurrentIndex(matched_idx)
            else:
                custom_target = WindowTarget(
                    title=f"Window {cfg.app_top_level_window} (Previous)",
                    handle_hex=cfg.app_top_level_window,
                    hwnd=0,
                    exe_name=""
                )
                self.combo_target.insertItem(1, custom_target.display_label(), custom_target)
                self.combo_target.setCurrentIndex(1)
        else:
            self.rb_window.setChecked(True)
            self.combo_target.setCurrentIndex(0)

        # Apply initial mode state
        self._on_mode_toggled(self.rb_window.isChecked())

    def _browse_app_path(self) -> None:
        self.rb_launch_exe.setChecked(True)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Target Application Executable",
            "C:\\Program Files",
            "Executables (*.exe);;All Files (*.*)"
        )
        if path:
            self.app_path_edit.setText(path)

    def check_server_status(self) -> None:
        url = self.appium_url_edit.text().strip() or "http://127.0.0.1:4723"

        # Prevent duplicate in-flight pings
        if self._ping_thread and self._ping_thread.isRunning():
            return

        if hasattr(self, "btn_test_url"):
            self.btn_test_url.setEnabled(False)
            self.btn_test_url.setText("Pinging...")

        self.lbl_status.setText(f"⏳ Pinging Appium server at {url}...")

        self._ping_thread = QThread(self)
        self._ping_worker = PingWorker(url)
        self._ping_worker.moveToThread(self._ping_thread)

        self._ping_thread.started.connect(self._ping_worker.run)
        self._ping_worker.finished.connect(self._on_ping_finished)
        self._ping_worker.finished.connect(self._ping_thread.quit)
        self._ping_thread.start()

    def _on_ping_finished(self, is_running: bool, msg: str) -> None:
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        if is_running:
            self.lbl_status.setText(f"[{now_str}] ✅ {msg}")
            self.lbl_status.setStyleSheet("background: #064e3b; color: #34d399; border: 1px solid #059669; border-radius: 6px; padding: 6px 10px; font-weight: 600; font-size: 11px;")
        else:
            self.lbl_status.setText(f"[{now_str}] ❌ {msg}")
            self.lbl_status.setStyleSheet("background: #450a0a; color: #f87171; border: 1px solid #dc2626; border-radius: 6px; padding: 6px 10px; font-weight: 600; font-size: 11px;")

        if hasattr(self, "btn_test_url"):
            self.btn_test_url.setEnabled(True)
            self.btn_test_url.setText("Ping Status")

    def reject(self) -> None:
        self._stop_ping_thread()
        super().reject()

    def closeEvent(self, event: object) -> None:
        self._stop_ping_thread()
        super().closeEvent(event)

    def close(self) -> bool:
        self._stop_ping_thread()
        return super().close()

    def _stop_ping_thread(self) -> None:
        if self._ping_worker:
            self._ping_worker.cancel()
            try:
                self._ping_worker.finished.disconnect()
            except Exception:
                pass
        if self._ping_thread and self._ping_thread.isRunning():
            self._ping_thread.quit()
            self._ping_thread.wait(600)
        self._ping_thread = None
        self._ping_worker = None

    def _on_connect_clicked(self) -> None:
        url = self.appium_url_edit.text().strip() or "http://127.0.0.1:4723"
        app_path = ""
        app_window = ""

        if self.rb_launch_exe.isChecked():
            app_path = self.app_path_edit.text().strip()
            if not app_path:
                self.lbl_status.setText("⚠️ Please specify an application executable path.")
                self.lbl_status.setStyleSheet("background: #451a03; color: #fbbf24; border: 1px solid #b45309; border-radius: 6px; padding: 6px 10px; font-weight: 600; font-size: 11px;")
                return
            session_name = Path(app_path).stem
        else:
            data = self.combo_target.currentData()
            if isinstance(data, WindowTarget):
                if data.is_root:
                    app_path = ""
                    session_name = "Desktop Root"
                else:
                    app_window = data.handle_hex
                    session_name = data.title or f"Window {data.handle_hex}"
            else:
                session_name = "Desktop Root"

        # Update config object
        self.config.appium_url = url
        self.config.app_path = app_path
        self.config.app_top_level_window = app_window
        self.config.confirm_disconnect = self.chk_confirm_disconnect.isChecked()
        self.config.confirm_reconnect_switch = self.chk_confirm_reconnect_switch.isChecked()

        # Add to recents
        recent = RecentSession(
            name=session_name,
            app_path=self.config.app_path,
            app_top_level_window=app_window,
            appium_url=url
        )
        ConfigManager.add_recent_session(self.config, recent)
        ConfigManager.save(self.config)

        self.session_requested.emit(self.config)
        self.accept()
