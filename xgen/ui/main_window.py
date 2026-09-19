"""
xGen Main Window.
Assembles three-panel inspector layout, toolbar, status bar, and coordinates core services.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
import sys
from typing import Optional
from PyQt6.QtCore import Qt, QPoint, QTimer, QEvent, QObject, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QCursor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from xgen.capture.inspect_mode import InspectMode
from xgen.capture.keyboard_hook import GlobalKeyHook
from xgen.capture.mouse_hook import MouseHook
from xgen.capture.overlay_window import OverlayWindow
from xgen.capture.transient_capture import TransientCapturer
from xgen.config import ConfigManager, XGenConfig
from xgen.core.driver_runner import DriverRunner
from xgen.core.element_bridge import ElementBridge
from xgen.core.session_manager import SessionManager, SessionState, WindowInfo
from xgen.core.tree_cache import TreeCacheStore
from xgen.core.tree_fetcher import TreeFetcher
from xgen.core.tree_parser import TreeParser, UINode
from xgen.core.uia_bridge import UIAElement
from xgen.events.event_bus import EventBus
from xgen.platform.factory import get_platform_backend
from xgen.ui.attribute_panel import AttributePanel
from xgen.ui.disambiguation_popup import DisambiguationPopup
from xgen.ui.legend_dialog import LegendDialog
from xgen.ui.session_dialog import SessionDialog
from xgen.ui.status_bar import StatusBar
from xgen.ui.theme import format_session_error, show_styled_message_box
from xgen.ui.toolbar import Toolbar
from xgen.ui.tree_panel import TreePanel
from xgen.ui.xpath_panel import XPathPanel
from xgen.utils.dpi import get_physical_cursor_pos, get_screen_dpr_at

logger = logging.getLogger("xgen.ui.main")


class MainWindow(QMainWindow):
    """
    Primary workspace window orchestrating the 3-panel layout, session lifecycle,
    and inspection capture hooks.
    """

    def __init__(self, config: XGenConfig, start_hooks: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("xGen — Windows XPath Inspector")
        self.setMinimumSize(850, 500)
        self.setStyleSheet("""
            QMainWindow { background: #0c0e12; color: #f1f5f9; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }
            QWidget { color: #f1f5f9; }
            QSplitter::handle { background: #181b22; width: 4px; }
            QSplitter::handle:hover { background: #3b82f6; }
            QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background: #0c0e12; border: none; }
            QAbstractScrollArea { background: #0c0e12; }
            QScrollBar:vertical { background: #0c0e12; width: 8px; margin: 0; }
            QScrollBar::handle:vertical { background: #262b36; min-height: 24px; border-radius: 4px; }
            QScrollBar::handle:vertical:hover { background: #3b82f6; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: none; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QScrollBar:horizontal { background: #0c0e12; height: 8px; margin: 0; }
            QScrollBar::handle:horizontal { background: #262b36; min-width: 24px; border-radius: 4px; }
            QScrollBar::handle:horizontal:hover { background: #3b82f6; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; background: none; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
            QToolTip { background: #1e2430; color: #f8fafc; border: 1px solid #3b82f6; border-radius: 4px; padding: 4px 8px; font-size: 11px; }
        """)

        # 1. Instantiate Core Services
        self.event_bus = EventBus.instance()
        self.session_manager = SessionManager(self)
        self.driver_runner = DriverRunner(self.session_manager, parent=self)
        self.tree_fetcher = TreeFetcher(self.session_manager, self.config, self)
        self.element_bridge = ElementBridge()

        # 2. Instantiate Capture Subsystem
        self.overlay = OverlayWindow()
        self.mouse_hook = MouseHook(self)
        self.key_hook = GlobalKeyHook(self)
        self.inspect_mode = InspectMode(self.config, overlay=self.overlay, mouse_hook=self.mouse_hook, parent=self)
        self.inspect_mode.set_window_filter(self._is_point_outside_xgen)
        self.transient_capturer = TransientCapturer(parent=self)

        # 3. Instantiate UI Panels
        self.toolbar = Toolbar(self)
        self.addToolBar(self.toolbar)

        self.tree_panel = TreePanel(self)
        self.attr_panel = AttributePanel(self)
        self.xpath_panel = XPathPanel(self)
        self.status_bar = StatusBar(self)
        self.setStatusBar(self.status_bar)

        self.disambiguation_popup = DisambiguationPopup(self)

        self._init_layout()
        self._wire_signals(start_hooks=start_hooks)
        self._setup_shortcuts()
        self._restore_window_state()
        self._refresh_window_picker()

        # Check for running Appium server and auto-connect on startup
        if self.config.auto_connect_on_startup:
            QTimer.singleShot(150, self._try_auto_connect)

    def _restore_window_state(self) -> None:
        """Restore window position, size, maximized state, and pin setting from config."""
        if self.config.window_x >= 0 and self.config.window_y >= 0:
            self.move(self.config.window_x, self.config.window_y)
        self.resize(self.config.window_width, self.config.window_height)

        if self.config.window_maximized:
            self.showMaximized()

        if self.config.pin_on_top:
            self.toolbar.btn_pin.setChecked(True)
            # Defer until after show() so winId() is valid and state is unified
            QTimer.singleShot(0, lambda: self._on_pin_toggled(True))

    def _init_layout(self) -> None:
        central_widget = QWidget(self)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(4, 4, 4, 4)

        self.setMinimumSize(920, 560)

        # Main Splitter: Left (TreePanel) | Right (RightSplitter: XPathPanel on Top + Collapsible AttributePanel on Bottom)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(True)
        self.splitter.setCollapsible(0, True)   # Allow tree panel to collapse to 0px when dragged past minimum
        self.splitter.setCollapsible(1, False)  # Right inspector panel should not collapse
        self.tree_panel.setMinimumWidth(350)    # Safe minimum width when open
        self.splitter.addWidget(self.tree_panel)

        # Right Vertical Splitter: Top (XPathPanel) | Bottom (AttributePanel)
        self.right_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_splitter.setHandleWidth(1)
        self.right_splitter.setChildrenCollapsible(False)
        self.right_splitter.setCollapsible(0, False)   # XPathPanel cannot be crushed
        self.right_splitter.setCollapsible(1, False)  # AttributePanel stops at 32px header, cannot collapse to 0px
        self.right_splitter.setMinimumWidth(480)
        self.xpath_panel.setMinimumWidth(320)
        self.xpath_panel.setMinimumHeight(200)        # Guarantees space to always show at least one full XPath card
        self.attr_panel.setMinimumHeight(32)          # Minimum height is the header bar
        self.attr_panel.setMaximumHeight(380)         # Upper cap so attributes cannot crush XPath panel
        self.right_splitter.addWidget(self.xpath_panel)
        self.right_splitter.addWidget(self.attr_panel)
        self.right_splitter.setStretchFactor(0, 7)
        self.right_splitter.setStretchFactor(1, 3)
        self.right_splitter.splitterMoved.connect(self._on_right_splitter_moved)

        self.splitter.addWidget(self.right_splitter)

        # Proportions: 40% Tree | 60% XPath + Attributes
        if self.config.splitter_sizes and len(self.config.splitter_sizes) == 2:
            self.splitter.setSizes(self.config.splitter_sizes)
        else:
            self.splitter.setStretchFactor(0, 4)
            self.splitter.setStretchFactor(1, 6)

        layout.addWidget(self.splitter)
        self.setCentralWidget(central_widget)

    def _wire_signals(self, start_hooks: bool = True) -> None:
        # Toolbar actions
        self.toolbar.fast_connect_requested.connect(self._on_fast_connect_requested)
        self.toolbar.connect_requested.connect(self._open_session_dialog)
        self.toolbar.disconnect_requested.connect(self._on_disconnect_requested)
        self.toolbar.refresh_requested.connect(self._on_refresh_requested)
        self.toolbar.inspect_toggled.connect(self._on_inspect_toggled)
        self.toolbar.window_switched.connect(self._on_window_switched)
        self.toolbar.export_xml_requested.connect(self._on_export_xml)
        self.toolbar.timed_capture_start.connect(self._on_start_timed_capture)
        self.toolbar.pin_toggled.connect(self._on_pin_toggled)
        self.toolbar.legend_requested.connect(self._open_legend_dialog)
        self.toolbar.custom_xpath_test.connect(self._on_custom_xpath_test)

        # Live Driver Testing & Action Signals
        self.xpath_panel.test_requested.connect(self._on_xpath_test_requested)
        self.xpath_panel.click_requested.connect(self._on_xpath_click_requested)
        self.xpath_panel.hover_requested.connect(self._on_xpath_hover_requested)
        self.xpath_panel.type_requested.connect(self._on_xpath_type_requested)
        self.xpath_panel.node_select_requested.connect(self._on_xpath_node_selected)
        self.driver_runner.test_finished.connect(self._on_driver_test_finished)
        self.driver_runner.action_completed.connect(self._on_driver_action_completed)

        # Transient capture signals
        self.transient_capturer.transient_captured.connect(self._on_transient_captured)
        self.transient_capturer.transient_failed.connect(self._on_transient_failed)
        self.transient_capturer.freeze_state_changed.connect(self.status_bar.show_freeze_state)
        self.transient_capturer.timed_capture_tick.connect(self._on_timed_tick)

        # Session signals
        self.session_manager.state_changed.connect(self._on_session_state_changed)
        self.session_manager.session_started.connect(self._on_session_started)
        self.session_manager.error_occurred.connect(self._on_session_error)

        # Tree fetcher signals
        def _on_tree_fetch_started(tier: int = 0) -> None:
            self.status_bar.show_progress(0, -1)
            self.toolbar.set_refreshing(True)

        self.tree_fetcher.fetch_started.connect(_on_tree_fetch_started)
        self.tree_fetcher.fetch_progress.connect(self.status_bar.show_progress)
        self.tree_fetcher.fetch_complete.connect(self._on_tree_fetch_complete)
        self.tree_fetcher.fetch_failed.connect(self._on_tree_fetch_failed)
        self.tree_fetcher.large_tree_warning.connect(self.status_bar.show_large_tree_warning)

        # Inspect mode & Selection
        self.inspect_mode.mode_changed.connect(self.toolbar.set_inspect_active)
        self.inspect_mode.hovering.connect(self.status_bar.show_hover_info)
        self.inspect_mode.element_clicked.connect(self._on_element_inspected)

        # Global OS-Level Keyboard Shortcuts (F3, F4, Esc, Ctrl+R work everywhere)
        self.key_hook.f3_pressed.connect(self.inspect_mode.toggle)
        self.key_hook.f4_pressed.connect(self._on_f4_freeze_shortcut)
        self.key_hook.esc_pressed.connect(self.inspect_mode.deactivate)
        self.key_hook.ctrl_r_pressed.connect(self._on_refresh_requested)
        if start_hooks:
            self.key_hook.start()

        # Tree selection
        self.tree_panel.node_selected.connect(self._on_node_selected_in_tree)

        # Attribute panel collapsible vertical space redistribution
        self.attr_panel.collapsed_toggled.connect(self._on_attr_panel_collapsed_toggled)

        # Disambiguation
        self.disambiguation_popup.node_chosen.connect(self._on_node_selected_in_tree)

    def _setup_shortcuts(self) -> None:
        # In-app application shortcuts (F3, F4, Esc are handled globally via GlobalKeyHook)
        self.sc_refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        self.sc_refresh.activated.connect(self._on_refresh_requested)

    def _open_session_dialog(self) -> None:
        dialog = SessionDialog(self.config, self.session_manager, self)
        dialog.session_requested.connect(self._on_session_dialog_requested)
        dialog.exec()

    def _on_session_dialog_requested(self, cfg: XGenConfig) -> None:
        self._refresh_window_picker(selected_handle=cfg.app_top_level_window)
        self.session_manager.connect(cfg)

    def _open_legend_dialog(self) -> None:
        """Open the interactive XPath Selector Quality Guide and Index Dialog."""
        dialog = LegendDialog(self)
        dialog.exec()

    def _on_fast_connect_requested(self) -> None:
        """Fast-Connect / Reconnect to target with current config or default root."""
        if self.session_manager.state in (SessionState.CONNECTING, SessionState.CONNECTED):
            return
        logger.info("Fast-connect requested via status dot.")
        self.status_bar.lbl_msg.setText("Connecting...")
        self.session_manager.connect(self.config)

    def _on_disconnect_requested(self) -> None:
        if self.session_manager.state != SessionState.CONNECTED:
            return
        if getattr(self.config, "confirm_disconnect", True):
            app_name = (self.session_manager.session_info.app_name if self.session_manager.session_info else "") or "current target"
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Disconnect Session?")
            msg_box.setText(
                f"Are you sure you want to disconnect from <b>{app_name}</b>?<br><br>"
                "This will terminate the active inspection session and clear the current tree."
            )
            msg_box.setIcon(QMessageBox.Icon.Question)
            msg_box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            msg_box.setDefaultButton(QMessageBox.StandardButton.Cancel)

            cb_remember = QCheckBox("Remember my choice (don't ask again)")
            cb_remember.setStyleSheet("QCheckBox { color: #cbd5e1; font-size: 11px; margin-top: 8px; }")
            msg_box.setCheckBox(cb_remember)

            reply = msg_box.exec()
            if reply != QMessageBox.StandardButton.Yes:
                return

            if cb_remember.isChecked():
                self.config.confirm_disconnect = False
                ConfigManager.save(self.config)

        logger.info("User confirmed session disconnect.")
        self.tree_fetcher.cancel()
        self.session_manager.disconnect()
        self.tree_panel.populate(None)
        self.attr_panel.clear()
        self.xpath_panel.clear()
        self.status_bar.lbl_msg.setText("Session disconnected.")

    def _try_auto_connect(self) -> None:
        """Probe Appium server on startup and connect automatically if active."""
        url = self.config.appium_url or "http://127.0.0.1:4723"
        is_running, msg = SessionManager.check_server_status(url, timeout_seconds=1.0)
        if is_running:
            logger.info("Appium server detected at %s on startup. Auto-connecting...", url)
            self.status_bar.lbl_msg.setText(f"Appium detected at {url}. Auto-connecting...")
            self.session_manager.connect(self.config)
        else:
            logger.debug("Appium server not running at %s on startup.", url)

    def _on_inspect_toggled(self, active: bool) -> None:
        if active:
            self.inspect_mode.activate()
        else:
            self.inspect_mode.deactivate()

    def _revert_window_picker(self, prev_handle: str) -> None:
        """Revert toolbar window picker selection back to prev_handle without triggering signals."""
        from xgen.utils.window_finder import normalize_handle
        norm_prev = normalize_handle(prev_handle)
        self.toolbar.combo_windows.blockSignals(True)
        matched_idx = 0
        for i in range(self.toolbar.combo_windows.count()):
            d = self.toolbar.combo_windows.itemData(i)
            if norm_prev is None:
                if not d or str(d).lower() in ("", "root"):
                    matched_idx = i
                    break
            elif normalize_handle(d) == norm_prev:
                matched_idx = i
                break
        self.toolbar.combo_windows.setCurrentIndex(matched_idx)
        self.toolbar.combo_windows.blockSignals(False)

    def _on_window_switched(self, handle: str) -> None:
        target_title = self.toolbar.combo_windows.currentText()
        if not self.session_manager.is_connected:
            if not handle or handle == "Root":
                self.config.app_top_level_window = ""
                self.config.app_path = ""
                self.status_bar.lbl_msg.setText("🎯 Target selected: Desktop Root. Click 🔴 Status Dot to connect.")
            else:
                self.config.app_top_level_window = handle
                self.config.app_path = ""
                self.status_bar.lbl_msg.setText(f"🎯 Target selected: {target_title}. Click 🔴 Status Dot to connect.")
            return

        from xgen.utils.window_finder import normalize_handle
        prev_handle = (self.session_manager.session_info.active_handle if self.session_manager.session_info else "") or self.config.app_top_level_window
        norm_target = normalize_handle(handle)
        norm_prev = normalize_handle(prev_handle)

        # If user re-selected the already active window/root, nothing to do
        if norm_target == norm_prev:
            return

        target_name = target_title or ("Desktop Root" if not handle or handle == "Root" else f"Window {handle}")

        if getattr(self.config, "confirm_reconnect_switch", True):
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Switch Window Context?")
            msg_box.setText(
                f"Appium will reconnect to switch window context to <b>{target_name}</b>.<br><br>"
                "Do you want to proceed?"
            )
            msg_box.setIcon(QMessageBox.Icon.Question)
            msg_box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            btn_allow = msg_box.button(QMessageBox.StandardButton.Ok)
            if btn_allow:
                btn_allow.setText("Allow")
            msg_box.setDefaultButton(QMessageBox.StandardButton.Ok)

            cb_remember = QCheckBox("Remember my choice (don't ask again)")
            cb_remember.setStyleSheet("QCheckBox { color: #cbd5e1; font-size: 11px; margin-top: 8px; }")
            msg_box.setCheckBox(cb_remember)

            reply = msg_box.exec()
            if reply != QMessageBox.StandardButton.Ok:
                # User cancelled: revert toolbar dropdown selection back to prev_handle
                self._revert_window_picker(prev_handle)
                return

            if cb_remember.isChecked():
                self.config.confirm_reconnect_switch = False
                ConfigManager.save(self.config)

        # Deactivate inspect mode if active
        if self.inspect_mode.is_active:
            self.inspect_mode.deactivate()
            self.toolbar.set_inspect_active(False)

        # Update config for the new target
        if not handle or handle == "Root":
            logger.info("Reconnecting session for Desktop Root context")
            self.config.app_top_level_window = ""
            self.config.app_path = ""
        else:
            logger.info("Reconnecting session for window handle: %s", handle)
            self.config.app_top_level_window = handle
            self.config.app_path = ""

        self.status_bar.lbl_msg.setText(f"Reconnecting to switch target to {target_name}...")
        self.tree_fetcher.cancel()
        self.session_manager.reconnect(self.config)

    def _on_refresh_requested(self) -> None:
        """Triggered on Ctrl+R or toolbar Refresh click: refresh tree and window list."""
        self.toolbar.set_refreshing(True)
        self.tree_fetcher.fetch_full()
        self._refresh_window_picker()

    def _refresh_window_picker(self, selected_handle: Optional[str] = None) -> None:
        """Scan open windows on OS and update toolbar window selector."""
        try:
            from xgen.utils.window_finder import get_open_windows
            windows = get_open_windows()

            if not selected_handle:
                if self.session_manager.is_connected and self.session_manager.session_info and self.session_manager.session_info.active_handle:
                    selected_handle = self.session_manager.session_info.active_handle
                elif self.config.app_top_level_window:
                    selected_handle = self.config.app_top_level_window

            self.toolbar.update_windows_list(windows, selected_handle=selected_handle)
        except Exception as e:
            logger.warning("Could not refresh window picker: %s", e)

    def _on_session_state_changed(self, state_str: str) -> None:
        app_name = self.session_manager.session_info.app_name if self.session_manager.session_info else ""
        self.toolbar.set_session_state(state_str, app_name)

    def _on_session_started(self, info) -> None:
        self.status_bar.lbl_msg.setText(f"Connected to {info.app_name}. Fetching initial UI tree...")
        target_handle = info.active_handle or self.config.app_top_level_window
        self._refresh_window_picker(selected_handle=target_handle)
        # Auto-fetch tree on session connect (force new fetch)
        self.tree_fetcher.fetch_full(target_handle, force=True)

    def _on_session_error(self, err: str) -> None:
        title, friendly_msg, tech_details = format_session_error(err)
        self.status_bar.lbl_msg.setText(f"Error: {title}")
        show_styled_message_box(
            self,
            title=title,
            text=friendly_msg,
            icon=QMessageBox.Icon.Warning,
            detailed_text=tech_details
        )

    def _on_tree_fetch_complete(self, handle: str, raw_xml: str, root: UINode) -> None:
        self._last_raw_xml = raw_xml
        self.toolbar.set_refreshing(False)
        self.status_bar.hide_progress()
        self.status_bar.lbl_msg.setText(f"Tree ready ({TreeParser.node_count(root):,} elements).")
        self.tree_panel.populate(root)

    def _on_tree_fetch_failed(self, err: str) -> None:
        self.toolbar.set_refreshing(False)
        self.status_bar.hide_progress()
        self.status_bar.lbl_msg.setText(f"Fetch failed: {err}")

    def _on_element_inspected(self, uia_el: UIAElement, click_x: int, click_y: int) -> None:
        self.toolbar.set_finding_xpath(True)
        label = uia_el.name or uia_el.control_type or "element"
        self.status_bar.lbl_msg.setText(f"⏳ Finding & verifying XPaths for '{label}'...")
        QApplication.processEvents()
        try:
            cache = TreeCacheStore.instance().get_active()
            bridge_res = self.element_bridge.find_node(uia_el, cache, self.session_manager, click_x=click_x, click_y=click_y)

            if not bridge_res.node:
                self.status_bar.lbl_msg.setText("⚠️ Element not found in UI tree snapshot.")
                return

            node = bridge_res.node
            self.attr_panel.populate(node)

            tree_root = cache.parsed_root if cache else None
            lxml_tree = cache.lxml_tree if cache else None
            self.xpath_panel.populate(node, tree_root=tree_root, lxml_tree=lxml_tree)

            age = cache.age_seconds if cache else 0
            self.status_bar.show_element_selected(node.tag, node.name, node.automation_id, age)

            # Highlight selected element in electric blue on target application window
            if node.bounding_rect:
                self.overlay.highlight_selected(node.bounding_rect)

            # Highlight in tree view
            if not bridge_res.is_live_fallback:
                self.tree_panel.expand_to_node(node)
            else:
                self.status_bar.lbl_msg.setText("⚠️ Live UIA — Element not in snapshot. Click [Refresh Tree] to update.")
        finally:
            self.toolbar.set_finding_xpath(False)

    def _on_node_selected_in_tree(self, node: UINode) -> None:
        self.toolbar.set_finding_xpath(True)
        label = node.name or node.tag or "element"
        self.status_bar.lbl_msg.setText(f"⏳ Generating XPaths for '{label}'...")
        QApplication.processEvents()
        try:
            self.attr_panel.populate(node)
            cache = TreeCacheStore.instance().get_active()
            tree_root = cache.parsed_root if cache else None
            lxml_tree = cache.lxml_tree if cache else None
            self.xpath_panel.populate(node, tree_root=tree_root, lxml_tree=lxml_tree)

            # Highlight selected node bounding box on screen
            if node and node.bounding_rect:
                self.overlay.highlight_selected(node.bounding_rect)
        finally:
            self.toolbar.set_finding_xpath(False)

    def _on_f4_freeze_shortcut(self) -> None:
        """Capture transient element under cursor without focus shift."""
        x, y = get_physical_cursor_pos()
        active_handle = self.session_manager.session_info.active_handle if self.session_manager.session_info else ""
        self.status_bar.lbl_msg.setText("Capturing transient snapshot (F4)...")
        self.transient_capturer.freeze_snapshot(x, y, active_handle)

    def _on_start_timed_capture(self, seconds: int) -> None:
        active_handle = self.session_manager.session_info.active_handle if self.session_manager.session_info else ""
        self.toolbar.set_timed_countdown(seconds)
        self.status_bar.lbl_msg.setText(f"⏱ Timed capture started: interact with target app ({seconds}s)...")
        self.transient_capturer.start_timed_capture(seconds, active_handle)

    def _on_timed_tick(self, seconds_left: int) -> None:
        self.toolbar.set_timed_countdown(seconds_left)
        if seconds_left > 0:
            self.status_bar.lbl_msg.setText(f"⏱ Capturing in {seconds_left}s... interact with your app now.")
        else:
            self.status_bar.lbl_msg.setText("⏱ Timed capture executing...")

    def _on_transient_captured(self, transient_root: UINode) -> None:
        self.toolbar.reset_timed_button()
        self.status_bar.lbl_msg.setText(f"⚡ Transient element '{transient_root.tag}' captured and merged into UI tree.")
        self.tree_panel.add_transient_nodes(transient_root)
        self._on_node_selected_in_tree(transient_root)

    def _on_transient_failed(self, reason: str) -> None:
        self.toolbar.reset_timed_button()
        self.status_bar.lbl_msg.setText(f"⚠️ Timed capture: {reason}")

    def _on_pin_toggled(self, on: bool) -> None:
        """Keep xGen floating on top of other windows (cross-platform via Qt)."""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    def _on_export_xml(self) -> None:
        """Export current page source XML to output/ directory parallel to the application."""
        cache = TreeCacheStore.instance().get_active()
        raw_xml = (cache.raw_xml if cache and cache.raw_xml else getattr(self, "_last_raw_xml", "")).strip()
        if not raw_xml:
            self.status_bar.lbl_msg.setText("⚠️ No page source available. Refresh tree first.")
            return

        try:
            if getattr(sys, "frozen", False):
                base_dir = Path(sys.executable).resolve().parent
            else:
                base_dir = Path(__file__).resolve().parents[2]

            out_dir = base_dir / "output"
            out_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            target_name = ""
            if self.session_manager.session_info and self.session_manager.session_info.app_name:
                sanitized = "".join(c for c in self.session_manager.session_info.app_name if c.isalnum() or c in ("-", "_")).strip()
                if sanitized:
                    target_name = f"_{sanitized}"

            filename = f"page_source_{timestamp}{target_name}.xml"
            filepath = out_dir / filename
            filepath.write_text(raw_xml, encoding="utf-8")

            rel_path = f"output/{filename}"
            logger.info("Exported raw XML page source to %s", filepath)
            self.status_bar.lbl_msg.setText(f"✅ Exported page source to {rel_path}")
        except Exception as e:
            logger.exception("Failed to export XML page source: %s", e)
            self.status_bar.lbl_msg.setText(f"❌ Failed to export XML: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Custom XPath Toolbar Input
    # ──────────────────────────────────────────────────────────────────────

    def _on_custom_xpath_test(self, _xpath_hint: str) -> None:
        """
        Opens the custom XPath tester popover dialog.
        The user enters the XPath inside the dialog itself.
        """
        self._show_custom_xpath_popover()

    def _show_custom_xpath_popover(self) -> None:
        """Opens the XPath tester dialog: editable XPath input + Test (cache) + Check in Appium + Click/Hover/Type actions."""
        from PyQt6.QtWidgets import QPushButton as _QPushButton

        dlg = QDialog(self)
        dlg.setWindowTitle("Test XPath — Action")
        dlg.setModal(False)  # Non-blocking so user can interact with the app
        dlg.setMinimumWidth(580)
        dlg.setStyleSheet("""
            QDialog { background: #0f1117; color: #f1f5f9;
                      font-family: 'Segoe UI', system-ui, sans-serif; }
            QLabel  { color: #cbd5e1; }
            QLineEdit { background: #1a1f2e; color: #e2e8f0; border: 1px solid #2a3140;
                        border-radius: 5px; padding: 4px 10px; font-size: 11px;
                        font-family: 'Cascadia Code', 'Consolas', monospace; }
            QLineEdit:focus { border-color: #6366f1; }
        """)

        root_layout = QVBoxLayout(dlg)
        root_layout.setContentsMargins(16, 14, 16, 14)
        root_layout.setSpacing(10)

        # ── XPath input row ──
        lbl_xpath = QLabel("XPath:")
        lbl_xpath.setStyleSheet("color: #94a3b8; font-size: 10px; font-weight: 600; letter-spacing: 0.5px;")
        root_layout.addWidget(lbl_xpath)

        input_row = QWidget()
        input_row.setStyleSheet("background: transparent;")
        input_row_layout = QHBoxLayout(input_row)
        input_row_layout.setContentsMargins(0, 0, 0, 0)
        input_row_layout.setSpacing(6)

        txt_xpath = QLineEdit()
        txt_xpath.setFixedHeight(28)
        txt_xpath.setPlaceholderText("e.g. //Button[@Name='OK'] or //Edit[@AutomationId='Search']")
        txt_xpath.setToolTip("Enter an XPath expression and press Enter or click Test")
        input_row_layout.addWidget(txt_xpath, 1)

        btn_test = _QPushButton("🧪 Test")
        btn_test.setFixedHeight(28)
        btn_test.setFixedWidth(70)
        btn_test.setToolTip("Evaluate XPath against cached in-memory UI tree (Instant)")
        btn_test.setStyleSheet(
            "QPushButton { background: #1e1545; color: #a78bfa; border: 1px solid #4c1d95;"
            "              border-radius: 6px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background: #2e1065; color: #c4b5fd; border-color: #7c3aed; }"
            "QPushButton:pressed { background: #3b0764; }"
            "QPushButton:disabled { color: #475569; border-color: #1e2430; background: #141820; }"
        )
        input_row_layout.addWidget(btn_test)
        root_layout.addWidget(input_row)

        # ── Status badge ──
        lbl_status = QLabel("🟡 Enter an XPath and click Test to evaluate against cached tree")
        lbl_status.setStyleSheet(
            "background: #1a1f2e; color: #94a3b8; border: 1px solid #2a3140;"
            "border-radius: 5px; padding: 5px 10px; font-size: 11px;"
        )
        lbl_status.setWordWrap(True)
        root_layout.addWidget(lbl_status)

        # ── Cached result area (populated dynamically after test) ──
        cached_results_container = QWidget()
        cached_results_container.setStyleSheet("background: transparent;")
        cached_results_layout = QVBoxLayout(cached_results_container)
        cached_results_layout.setContentsMargins(0, 0, 0, 0)
        cached_results_layout.setSpacing(4)
        cached_results_container.setVisible(False)
        root_layout.addWidget(cached_results_container)

        # ── Type text input (hidden until Type clicked) ──
        type_row = QWidget()
        type_row.setStyleSheet("background: transparent;")
        type_row_layout = QHBoxLayout(type_row)
        type_row_layout.setContentsMargins(0, 0, 0, 0)
        type_row_layout.setSpacing(6)
        lbl_type_lbl = QLabel("Text:")
        lbl_type_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        lbl_type_lbl.setFixedWidth(36)
        type_row_layout.addWidget(lbl_type_lbl)
        txt_type_input = QLineEdit()
        txt_type_input.setPlaceholderText("Text to type into the element...")
        type_row_layout.addWidget(txt_type_input, 1)
        type_row.setVisible(False)
        root_layout.addWidget(type_row)

        # ── Action result label ──
        lbl_action_result = QLabel("")
        lbl_action_result.setWordWrap(True)
        lbl_action_result.setVisible(False)
        root_layout.addWidget(lbl_action_result)

        # ── Action buttons row ──
        btn_row = QWidget()
        btn_row.setStyleSheet("background: transparent;")
        btn_row_layout = QHBoxLayout(btn_row)
        btn_row_layout.setContentsMargins(0, 4, 0, 0)
        btn_row_layout.setSpacing(8)

        BTN_STYLE = (
            "QPushButton {{ background: {bg}; color: {fg}; border: 1px solid {border};"
            " border-radius: 6px; padding: 4px 13px; font-size: 11px; font-weight: 600; }}"
            "QPushButton:hover {{ background: {hover}; }}"
            "QPushButton:disabled {{ color: #475569; border-color: #1e2430; background: #141820; }}"
        )

        btn_check_appium = _QPushButton("📡 Check in Appium")
        btn_check_appium.setFixedHeight(28)
        btn_check_appium.setEnabled(False)
        btn_check_appium.setToolTip("Query live Appium session for element existence and response latency")
        btn_check_appium.setStyleSheet(BTN_STYLE.format(bg="#1e1b4b", fg="#a5b4fc", border="#4338ca", hover="#312e81"))
        btn_row_layout.addWidget(btn_check_appium)

        btn_click = _QPushButton("👆 Click")
        btn_click.setFixedHeight(28)
        btn_click.setEnabled(False)
        btn_click.setToolTip("Click the element via Appium")
        btn_click.setStyleSheet(BTN_STYLE.format(bg="#1e293b", fg="#38bdf8", border="#0284c7", hover="#0369a1"))
        btn_row_layout.addWidget(btn_click)

        btn_hover = _QPushButton("🎯 Hover")
        btn_hover.setFixedHeight(28)
        btn_hover.setEnabled(False)
        btn_hover.setToolTip("Hover mouse over the element")
        btn_hover.setStyleSheet(BTN_STYLE.format(bg="#1e293b", fg="#a78bfa", border="#7c3aed", hover="#5b21b6"))
        btn_row_layout.addWidget(btn_hover)

        btn_type = _QPushButton("⌨️ Type")
        btn_type.setFixedHeight(28)
        btn_type.setEnabled(False)
        btn_type.setToolTip("Type text into the element via Appium")
        btn_type.setStyleSheet(BTN_STYLE.format(bg="#1e293b", fg="#34d399", border="#059669", hover="#047857"))
        btn_row_layout.addWidget(btn_type)

        btn_row_layout.addStretch(1)

        btn_close = _QPushButton("Close")
        btn_close.setFixedHeight(28)
        btn_close.setStyleSheet(
            "QPushButton { background: #1e2430; color: #64748b; border: 1px solid #2a3140;"
            " border-radius: 6px; padding: 4px 14px; font-size: 11px; }"
            "QPushButton:hover { color: #94a3b8; border-color: #3b4b6b; }"
        )
        btn_close.clicked.connect(dlg.close)
        btn_row_layout.addWidget(btn_close)
        root_layout.addWidget(btn_row)

        # ── State ──
        _is_busy = [False]
        _type_visible = [False]
        _current_xpath = [""]

        # ── Worker signal bridge (marshals background thread back to Qt GUI thread) ──
        class _CustomXPathWorker(QObject):
            test_finished = pyqtSignal(str, object)

        worker = _CustomXPathWorker(dlg)

        def _set_status(text: str, color: str = "#94a3b8", bg: str = "#1a1f2e", border: str = "#2a3140") -> None:
            lbl_status.setText(text)
            lbl_status.setStyleSheet(
                f"background: {bg}; color: {color}; border: 1px solid {border};"
                "border-radius: 5px; padding: 5px 10px; font-size: 11px;"
            )

        def _set_action_result(text: str, success: bool) -> None:
            lbl_action_result.setVisible(True)
            lbl_action_result.setText(text)
            lbl_action_result.setStyleSheet(
                f"background: {'#064e3b' if success else '#450a0a'};"
                f" color: {'#34d399' if success else '#f87171'};"
                " border: 1px solid;"
                f" border-color: {'#059669' if success else '#dc2626'};"
                " border-radius: 5px; padding: 5px 10px; font-size: 11px; font-weight: 600;"
            )

        def _set_busy(busy: bool, current_action: str = "") -> None:
            """Prevent spamming: lock all controls while any operation is in-flight."""
            _is_busy[0] = busy
            btn_test.setEnabled(not busy)
            txt_xpath.setEnabled(not busy)

            if busy:
                btn_check_appium.setEnabled(False)
                btn_click.setEnabled(False)
                btn_hover.setEnabled(False)
                btn_type.setEnabled(False)
                if current_action == "check":
                    btn_check_appium.setText("⏳ Checking...")
                elif current_action == "click":
                    btn_click.setText("⏳ Clicking...")
                elif current_action == "hover":
                    btn_hover.setText("⏳ Hovering...")
                elif current_action == "type":
                    btn_type.setText("⏳ Typing...")
            else:
                btn_check_appium.setText("📡 Check in Appium")
                btn_click.setText("👆 Click")
                btn_hover.setText("🎯 Hover")
                btn_type.setText("⌨️ Type")
                is_conn = self.session_manager.is_connected
                has_xpath = bool(_current_xpath[0])
                btn_check_appium.setEnabled(has_xpath and is_conn)
                btn_click.setEnabled(has_xpath and is_conn)
                btn_hover.setEnabled(has_xpath and is_conn)
                btn_type.setEnabled(has_xpath and is_conn)

        def _clear_cached_results() -> None:
            while cached_results_layout.count():
                item = cached_results_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            cached_results_container.setVisible(False)

        def _show_cached_results(nodes: list) -> None:
            _clear_cached_results()
            if not nodes:
                return
            lbl_ct = QLabel(f"📋 Cached tree: {len(nodes)} match{'es' if len(nodes) != 1 else ''}")
            lbl_ct.setStyleSheet("color: #10b981; font-size: 10px; font-weight: 600;")
            cached_results_layout.addWidget(lbl_ct)
            for node in nodes[:3]:
                desc = f"  {node.tag}  Name='{node.name}'  AutomationId='{node.automation_id}'"
                lbl_n = QLabel(desc)
                lbl_n.setStyleSheet(
                    "background: #0a1a14; color: #6ee7b7; border: 1px solid #065f46;"
                    "border-radius: 4px; padding: 4px 8px; font-size: 10px;"
                    "font-family: 'Cascadia Code', 'Consolas', monospace;"
                )
                cached_results_layout.addWidget(lbl_n)
            cached_results_container.setVisible(True)
            dlg.adjustSize()

        def _run_test() -> None:
            """Evaluate XPath ONLY against local cached UI tree (synchronous & instant)."""
            if _is_busy[0]:
                return
            xpath = txt_xpath.text().strip()
            if not xpath:
                txt_xpath.setFocus()
                return

            _current_xpath[0] = xpath
            lbl_action_result.setVisible(False)
            type_row.setVisible(False)
            _type_visible[0] = False
            _clear_cached_results()

            self.status_bar.lbl_msg.setText(f"🔍 Evaluating: {xpath[:60]}...")

            # --- Cached lxml tree lookup ---
            cache = TreeCacheStore.instance().get_active()
            matched_nodes: list = []

            if cache and cache.lxml_tree is not None:
                try:
                    results = cache.lxml_tree.xpath(xpath)
                    if results:
                        def _lxml_to_uinode(lxml_el, parsed_root) -> Optional[UINode]:
                            tag = lxml_el.tag
                            aid = lxml_el.get("AutomationId", "")
                            name_attr = lxml_el.get("Name", "")
                            stack = [parsed_root]
                            while stack:
                                n = stack.pop()
                                if n.tag == tag:
                                    if (aid and n.automation_id == aid) or (name_attr and n.name == name_attr) or (not aid and not name_attr):
                                        return n
                                stack.extend(reversed(n.children))
                            return None

                        for lxml_el in results:
                            if cache.parsed_root:
                                node = _lxml_to_uinode(lxml_el, cache.parsed_root)
                                if node:
                                    matched_nodes.append(node)
                except Exception as exc:
                    logger.debug("Custom XPath lxml eval error: %s", exc)

            is_conn = self.session_manager.is_connected

            if matched_nodes:
                _show_cached_results(matched_nodes)
                if matched_nodes[0].bounding_rect:
                    self.overlay.highlight_selected(matched_nodes[0].bounding_rect)
                ct_str = f"{len(matched_nodes)} match{'es' if len(matched_nodes) != 1 else ''}"
                if is_conn:
                    _set_status(
                        f"✅ Found {ct_str} in cached tree. Ready to 'Check in Appium' or perform actions.",
                        color="#34d399", bg="#064e3b", border="#059669"
                    )
                else:
                    _set_status(
                        f"📋 Found {ct_str} in cached tree. Connect an Appium session to run live actions.",
                        color="#fbbf24", bg="#1c1a07", border="#92400e"
                    )
            else:
                if is_conn:
                    _set_status(
                        "❌ No match in cached tree. Click 'Check in Appium' to test live on app.",
                        color="#fbbf24", bg="#1c1a07", border="#92400e"
                    )
                else:
                    _set_status(
                        "❌ No match in cached tree. Connect a session or refresh the tree.",
                        color="#f87171", bg="#450a0a", border="#dc2626"
                    )

            # Enable action buttons if connected
            btn_check_appium.setEnabled(is_conn)
            btn_click.setEnabled(is_conn)
            btn_hover.setEnabled(is_conn)
            btn_type.setEnabled(is_conn)

        def _on_check_appium() -> None:
            """Query Appium explicitly on background thread."""
            if _is_busy[0]:
                return
            xpath = _current_xpath[0] or txt_xpath.text().strip()
            if not xpath:
                txt_xpath.setFocus()
                return
            _current_xpath[0] = xpath

            if not self.session_manager.is_connected:
                _set_status(
                    "⚠️ No active Appium session. Connect to a target first.",
                    color="#fbbf24", bg="#1c1a07", border="#92400e"
                )
                return

            _set_busy(True, current_action="check")
            _set_status("⏳ Querying Appium driver...")
            lbl_action_result.setVisible(False)

            import threading as _threading
            def _live():
                try:
                    res = self.driver_runner.test_xpath(xpath)
                except Exception as exc:
                    from xgen.core.driver_runner import TestElementResult
                    res = TestElementResult(success=False, error_message=str(exc))
                try:
                    worker.test_finished.emit(xpath, res)
                except (RuntimeError, Exception):
                    pass

            _threading.Thread(target=_live, daemon=True).start()

        def _on_worker_test_finished(tested_xpath: str, res: object) -> None:
            try:
                _set_busy(False)
                if not dlg.isVisible() or _current_xpath[0] != tested_xpath:
                    return
                if res.success:
                    _set_status(
                        f"✅ Found via Appium ({res.duration_ms:.0f} ms)",
                        color="#34d399", bg="#064e3b", border="#059669"
                    )
                    if res.bounding_rect:
                        self.overlay.highlight_tested(res.bounding_rect)
                else:
                    _set_status(
                        f"❌ Not found via Appium: {res.error_message}",
                        color="#f87171", bg="#450a0a", border="#dc2626"
                    )
            except RuntimeError:
                pass

        worker.test_finished.connect(_on_worker_test_finished)

        # ── Trigger test ──
        btn_test.clicked.connect(_run_test)
        txt_xpath.returnPressed.connect(_run_test)
        btn_check_appium.clicked.connect(_on_check_appium)

        # ── Action slot ──
        def _on_action_completed(action: str, success: bool, msg: str) -> None:
            try:
                _set_busy(False)
                if not dlg.isVisible():
                    return
                icon = "✅" if success else "❌"
                _set_action_result(f"{icon} {action}: {msg}", success)
            except RuntimeError:
                pass

        self.driver_runner.action_completed.connect(_on_action_completed)

        def _cleanup(*_args) -> None:
            try:
                self.driver_runner.action_completed.disconnect(_on_action_completed)
            except (RuntimeError, TypeError):
                pass

        dlg.finished.connect(_cleanup)

        def _on_click() -> None:
            if _is_busy[0]:
                return
            xpath = _current_xpath[0] or txt_xpath.text().strip()
            if not xpath:
                txt_xpath.setFocus()
                return
            _current_xpath[0] = xpath
            _set_busy(True, current_action="click")
            lbl_action_result.setVisible(False)
            self.driver_runner.async_click_xpath(xpath)

        def _on_hover() -> None:
            if _is_busy[0]:
                return
            xpath = _current_xpath[0] or txt_xpath.text().strip()
            if not xpath:
                txt_xpath.setFocus()
                return
            _current_xpath[0] = xpath
            _set_busy(True, current_action="hover")
            lbl_action_result.setVisible(False)
            self.driver_runner.async_hover_xpath(xpath)

        def _on_type() -> None:
            if _is_busy[0]:
                return
            xpath = _current_xpath[0] or txt_xpath.text().strip()
            if not xpath:
                txt_xpath.setFocus()
                return
            _current_xpath[0] = xpath
            if not _type_visible[0]:
                _type_visible[0] = True
                type_row.setVisible(True)
                dlg.adjustSize()
                txt_type_input.setFocus()
                return
            text = txt_type_input.text()
            if not text:
                txt_type_input.setFocus()
                return
            _set_busy(True, current_action="type")
            lbl_action_result.setVisible(False)
            self.driver_runner.async_send_keys_xpath(xpath, text)

        btn_click.clicked.connect(_on_click)
        btn_hover.clicked.connect(_on_hover)
        btn_type.clicked.connect(_on_type)
        txt_type_input.returnPressed.connect(_on_type)

        # Focus input and open
        dlg.adjustSize()
        txt_xpath.setFocus()
        dlg.show()

    def changeEvent(self, event: object) -> None:
        """Auto-deactivate inspect mode when xGen is minimized."""
        if isinstance(event, QEvent) and event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized() and self.inspect_mode.is_active:
                self.inspect_mode.deactivate()
        super().changeEvent(event)

    def _is_point_outside_xgen(self, screen_x: int, screen_y: int) -> bool:
        """
        Returns True if screen coordinate is outside xGen's own window geometry
        AND not on the Windows Taskbar / System Tray.
        """
        # 1. Native process check: if the window under cursor belongs to our own PID, NEVER suppress!
        backend = get_platform_backend()
        try:
            hwnd = backend.window_from_point(screen_x, screen_y)
            if hwnd:
                pid = backend.get_process_id_for_window(hwnd)
                if pid == os.getpid():
                    return False
        except Exception:
            pass

        # 2. Check if click is inside xGen's own window geometry
        dpr = get_screen_dpr_at(screen_x, screen_y)
        lx = int(screen_x / dpr) if dpr > 0 else screen_x
        ly = int(screen_y / dpr) if dpr > 0 else screen_y

        geom = self.frameGeometry()
        is_inside = (geom.left() <= lx <= geom.right() and geom.top() <= ly <= geom.bottom()) or \
                    (geom.left() <= screen_x <= geom.right() and geom.top() <= screen_y <= geom.bottom())
        if is_inside:
            return False

        # 3. Check if click is on the Windows Taskbar / System Tray
        app = QApplication.instance()
        if app:
            screen = app.screenAt(QPoint(lx, ly)) or app.primaryScreen()
            if screen:
                avail = screen.availableGeometry()
                # If coordinate is outside available work area (e.g. on Taskbar), NEVER suppress clicks!
                if not (avail.left() <= lx <= avail.right() and avail.top() <= ly <= avail.bottom()):
                    return False

        return True

    def _on_xpath_test_requested(self, xpath: str, card: object) -> None:
        """Executes live Appium driver element test asynchronously on background thread."""
        self.driver_runner.async_test_xpath(xpath, card)

    def _on_driver_test_finished(self, res: object, card: object) -> None:
        """Slot invoked safely on main Qt GUI thread upon driver test completion."""
        # 1. Update UI card if still valid
        try:
            if hasattr(card, "show_test_result") and hasattr(res, "success"):
                card.show_test_result(res.success, res.duration_ms, res.error_message)
        except RuntimeError:
            logger.debug("Card widget was destroyed before async driver test finished.")

        # 2. Highlight tested element on screen
        try:
            if getattr(res, "success", False):
                rect = getattr(res, "bounding_rect", None)
                # Fallback to candidate / target node bounding rect if driver rect endpoint was not returned
                if rect is None and card is not None:
                    target_node = getattr(card, "target_node", None)
                    if target_node and target_node.bounding_rect:
                        rect = target_node.bounding_rect
                    elif hasattr(card, "candidate") and getattr(card.candidate, "verify_result", None):
                        matched = card.candidate.verify_result.matched_nodes
                        if matched and matched[0].bounding_rect:
                            rect = matched[0].bounding_rect

                if rect:
                    self.overlay.highlight_tested(rect)
        except (RuntimeError, Exception) as e:
            logger.debug("Suppressed error in test overlay highlight: %s", e)

    def _on_xpath_node_selected(self, node: UINode) -> None:
        """Invoked when user clicks [Select in Tree] on a matched element in the XPath panel."""
        if not node:
            return
        self.tree_panel.select_node(node)
        self.attr_panel.populate(node)
        if node.bounding_rect:
            self.overlay.highlight_selected(node.bounding_rect)

    def _on_xpath_click_requested(self, xpath: str, card: object) -> None:
        """Executes live click on element via Appium asynchronously on background thread."""
        self._last_action_card = card
        self.driver_runner.async_click_xpath(xpath)

    def _on_xpath_hover_requested(self, xpath: str, card: object) -> None:
        """Executes live hover on element via Appium asynchronously on background thread."""
        self._last_action_card = card
        self.driver_runner.async_hover_xpath(xpath)

    def _on_xpath_type_requested(self, xpath: str, text: str, card: object) -> None:
        """Executes live typing on element via Appium asynchronously on background thread."""
        self._last_action_card = card
        self.driver_runner.async_send_keys_xpath(xpath, text)

    def _on_driver_action_completed(self, action: str, success: bool, msg: str) -> None:
        icon = "✅" if success else "❌"
        self.status_bar.lbl_msg.setText(f"{icon} {action}: {msg}")
        if hasattr(self, "_last_action_card") and self._last_action_card:
            try:
                card = self._last_action_card
                if hasattr(card, "btn_click") and hasattr(card, "lbl_live_status"):
                    card.btn_click.setEnabled(True)
                    card.btn_click.setText("👆 Click")
                    card.btn_hover.setEnabled(True)
                    card.btn_hover.setText("🎯 Hover")
                    card.btn_type.setEnabled(True)
                    card.btn_type.setText("⌨️ Type")

                    if success:
                        if action == "Click":
                            card.lbl_live_status.setText("🟢 Clicked Successfully")
                        elif action == "Hover":
                            card.lbl_live_status.setText("🟢 Hovered Element")
                        elif action == "Type":
                            card.lbl_live_status.setText("🟢 Typed Successfully")
                        else:
                            card.lbl_live_status.setText(f"🟢 {action} Done")
                        card.lbl_live_status.setStyleSheet("background: #064e3b; color: #34d399; border: 1px solid #059669; border-radius: 4px; padding: 3px 8px; font-size: 10px; font-weight: 600;")
                    else:
                        card.lbl_live_status.setText(f"🔴 {action} failed: {msg}")
                        card.lbl_live_status.setStyleSheet("background: #450a0a; color: #f87171; border: 1px solid #dc2626; border-radius: 4px; padding: 3px 8px; font-size: 10px; font-weight: 600;")
            except Exception:
                pass

    def _on_right_splitter_moved(self, pos: int, index: int) -> None:
        """Auto-collapse/expand attribute panel visually when dragged past safe threshold (95px minimum for 1 table entry)."""
        sizes = self.right_splitter.sizes()
        if len(sizes) != 2:
            return

        attr_h = sizes[1]
        # Dragged below 95px (cannot comfortably show 1 table entry) -> Auto-collapse table into header bar!
        if not self.attr_panel.is_collapsed and attr_h < 95:
            total = sizes[0] + sizes[1]
            self._saved_right_splitter_sizes = [max(200, total - 250), 250]
            self.attr_panel.set_collapsed(True, emit_signal=False)
        # Dragged up above 95px (enough height for column headers + 1 full row) -> Auto-expand table!
        elif self.attr_panel.is_collapsed and attr_h >= 95:
            self.attr_panel.set_collapsed(False, emit_signal=False)

    def _on_attr_panel_collapsed_toggled(self, is_collapsed: bool) -> None:
        """Dynamically redistribute vertical splitter space when attribute panel collapses or expands."""
        current_sizes = self.right_splitter.sizes()
        total_h = sum(current_sizes) if current_sizes else 1000
        if is_collapsed:
            if len(current_sizes) == 2 and current_sizes[1] > 60:
                self._saved_right_splitter_sizes = current_sizes
            self.right_splitter.setSizes([total_h - 32, 32])
        else:
            if hasattr(self, "_saved_right_splitter_sizes") and self._saved_right_splitter_sizes:
                self.right_splitter.setSizes(self._saved_right_splitter_sizes)
            else:
                target_attr = min(260, total_h // 3)
                self.right_splitter.setSizes([total_h - target_attr, target_attr])

    def closeEvent(self, event: QCloseEvent) -> None:
        """Gracefully release threads, hooks, and save state on application exit."""
        self.key_hook.stop()
        self.inspect_mode.deactivate()
        self.transient_capturer.cancel_timed_capture()
        self.toolbar.reset_timed_button()
        self.overlay.close()
        self.tree_fetcher.close()
        self.session_manager.close()

        # Save layout and geometry state
        if not self.isMaximized():
            self.config.window_x = self.x()
            self.config.window_y = self.y()
            self.config.window_width = self.width()
            self.config.window_height = self.height()
        self.config.window_maximized = self.isMaximized()
        self.config.splitter_sizes = self.splitter.sizes()
        self.config.pin_on_top = self.toolbar.btn_pin.isChecked()
        ConfigManager.save(self.config)

        event.accept()
