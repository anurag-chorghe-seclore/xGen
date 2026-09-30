"""
xGen Main Toolbar Widget.
Provides session indicator, window switcher, tree refresh, inspect toggle, freeze toggle, timed capture triggers, and custom XPath input.
"""

from __future__ import annotations

from typing import Any, List, Optional
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolBar,
    QWidget,
)

from xgen.core.session_manager import SessionState, WindowInfo


class StatusDot(QPushButton):
    """
    Precision-rendered, mathematically centered status dot with concentric ring.
    Eliminates text emoji glyph font baseline offsets completely.
    """
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dot_color = QColor("#ef4444")
        self._bg_color = QColor("#201314")
        self._border_color = QColor("#7f1d1d")
        self._hover_bg = QColor("#2d1618")
        self._hover_border = QColor("#ef4444")
        self._is_hovered = False
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def enterEvent(self, event) -> None:
        self._is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._is_hovered = False
        self.update()
        super().leaveEvent(event)

    def set_colors(self, dot: str, bg: str, border: str, hover_bg: str, hover_border: str) -> None:
        self._dot_color = QColor(dot)
        self._bg_color = QColor(bg)
        self._border_color = QColor(border)
        self._hover_bg = QColor(hover_bg)
        self._hover_border = QColor(hover_border)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = self._hover_bg if self._is_hovered else self._bg_color
        border = self._hover_border if self._is_hovered else self._border_color

        w = self.width()
        h = self.height()

        # Outer concentric circle (20x20 centered in 24x24)
        painter.setPen(QPen(border, 1))
        painter.setBrush(QBrush(bg))
        painter.drawEllipse(2, 2, w - 4, h - 4)

        # Inner solid dot (8x8 centered in 24x24: 8px margin all sides)
        cx = w // 2
        cy = h // 2
        r = 4
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._dot_color))
        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)


class Toolbar(QToolBar):
    """Application top toolbar with action triggers and context selectors."""
    connect_requested = pyqtSignal()           # open full session dialog
    fast_connect_requested = pyqtSignal()      # quick toggle connect
    disconnect_requested = pyqtSignal()        # quick disconnect
    refresh_requested = pyqtSignal()
    inspect_toggled = pyqtSignal(bool)
    window_switched = pyqtSignal(str)          # handle
    export_xml_requested = pyqtSignal()        # export raw XML page source
    timed_capture_start = pyqtSignal(int)      # delay seconds
    pin_toggled = pyqtSignal(bool)             # always on top
    legend_requested = pyqtSignal()            # open XPath legend guide dialog
    custom_xpath_test = pyqtSignal(str)        # user-entered XPath string

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Main Toolbar", parent)
        self.setMovable(False)
        self.setFloatable(False)
        self._current_state = SessionState.DISCONNECTED.value
        self._current_app_name = ""
        self._init_ui()

    def _init_ui(self) -> None:
        self.setStyleSheet("QToolBar { background: #12151b; border-bottom: 1px solid #1e222b; spacing: 0px; padding: 2px 0px; }")
        container = QWidget(self)
        container.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(0)

        # === 1. Left Section: Session Dot, Settings, and Window Target ===
        self.left_container = QWidget()
        self.left_container.setStyleSheet("background: transparent;")
        self.left_container.setFixedWidth(350)  # Matches minimum width of Tree Panel (350px)
        left_layout = QHBoxLayout(self.left_container)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(6)

        # 1a. Precision Vector Status Dot (Fast Connect / Disconnect)
        self.btn_status_dot = StatusDot()
        self.btn_status_dot.setToolTip("Disconnected\nClick to Quick-Connect to last target")
        self.btn_status_dot.clicked.connect(self._on_status_dot_clicked)
        left_layout.addWidget(self.btn_status_dot)

        # 1b. Dedicated Session Management / Config Button
        self.btn_session_config = QPushButton("⚙️ Session")
        self.btn_session_config.setFixedHeight(26)
        self.btn_session_config.setFixedWidth(82)
        self.btn_session_config.setToolTip("Configure Appium session, capabilities, and target apps")
        self.btn_session_config.setStyleSheet(
            "QPushButton { background: #181c24; color: #cbd5e1; border: 1px solid #2a3140; border-radius: 6px; padding: 2px 8px 3px 8px; font-size: 11px; font-weight: 500; text-align: center; }"
            "QPushButton:hover { background: #222834; color: #ffffff; border-color: #3b82f6; }"
        )
        self.btn_session_config.clicked.connect(self.connect_requested.emit)
        left_layout.addWidget(self.btn_session_config)

        # 1c. Window Switcher Dropdown (Fills 222px across the 350px container)
        self.combo_windows = QComboBox()
        self.combo_windows.setFixedHeight(26)
        self.combo_windows.setFixedWidth(222)
        self.combo_windows.setToolTip("Switch active window inspection target\n(Note: Switching to an app needs Refresh to update UI tree)")
        self.combo_windows.setStyleSheet(
            "QComboBox { background: #181c24; color: #f1f5f9; border: 1px solid #2a3140; border-radius: 6px; padding: 1px 10px; font-size: 11px; }"
            "QComboBox:hover { border-color: #3b82f6; }"
            "QComboBox::drop-down { border: none; width: 16px; }"
            "QComboBox QAbstractItemView { background: #14171e; color: #cbd5e1; selection-background-color: #2563eb; selection-color: #ffffff; border: 1px solid #28303f; border-radius: 6px; padding: 4px; outline: none; }"
            "QComboBox QAbstractItemView::item { min-height: 24px; padding: 4px 10px; border-radius: 4px; border-bottom: 1px solid #1e2430; margin: 1px 0px; }"
            "QComboBox QAbstractItemView::item:hover { background-color: #1e2533; color: #ffffff; }"
            "QComboBox QAbstractItemView::item:selected { background-color: #2563eb; color: #ffffff; font-weight: 500; }"
        )
        self.combo_windows.currentIndexChanged.connect(self._on_window_selected)
        if self.combo_windows.view() and self.combo_windows.view().window():
            self.combo_windows.view().window().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.combo_windows.view().window().setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        left_layout.addWidget(self.combo_windows)

        layout.addWidget(self.left_container)

        # Space before center buttons
        layout.addStretch(1)

        # === 2. Center Section: Primary Action & Inspection Tools ===
        self.center_container = QWidget()
        self.center_container.setStyleSheet("background: transparent;")
        center_layout = QHBoxLayout(self.center_container)
        center_layout.setContentsMargins(8, 0, 8, 0)
        center_layout.setSpacing(6)

        # 2a. Refresh Tree Button (Placed first on left of Inspect)
        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.setFixedHeight(26)
        self.btn_refresh.setToolTip("Fetch fresh UI tree snapshot (Ctrl+R)")
        self.btn_refresh.setStyleSheet(
            "QPushButton { background: #181c24; color: #cbd5e1; border: 1px solid #2a3140; border-radius: 6px; padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 500; text-align: center; }"
            "QPushButton:hover { background: #222834; color: #ffffff; border-color: #3b82f6; }"
        )
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        center_layout.addWidget(self.btn_refresh)

        # 2b. Inspect Toggle Button (F3)
        self.btn_inspect = QPushButton("🎯 Inspect (F3)")
        self.btn_inspect.setFixedHeight(26)
        self.btn_inspect.setCheckable(True)
        self.btn_inspect.setToolTip("Toggle Inspect Hover Mode (F3)")
        self.btn_inspect.setStyleSheet(
            "QPushButton { background: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 6px; padding: 2px 13px 3px 13px; font-weight: 600; font-size: 11px; text-align: center; }"
            "QPushButton:hover { background: #0284c7; color: #ffffff; }"
            "QPushButton:checked { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #dc2626, stop:1 #ef4444); color: #ffffff; border: 1px solid #f87171; }"
            "QPushButton:checked:hover { background: #b91c1c; }"
        )
        self.btn_inspect.toggled.connect(self._on_inspect_clicked)
        center_layout.addWidget(self.btn_inspect)

        # 2c. Timed Capture Button
        self.btn_timed = QPushButton("⏱ 5s")
        self.btn_timed.setFixedHeight(26)
        self.btn_timed.setToolTip("Set 5-second countdown to interact with app before auto-capture")
        self.btn_timed.setStyleSheet(
            "QPushButton { background: #181c24; color: #cbd5e1; border: 1px solid #2a3140; border-radius: 6px; padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 500; text-align: center; }"
            "QPushButton:hover { background: #222834; color: #ffffff; border-color: #3b82f6; }"
        )
        self.btn_timed.clicked.connect(lambda: self.timed_capture_start.emit(5))
        center_layout.addWidget(self.btn_timed)

        # 2d. Pin on Top (Always on Top) Toggle
        self.btn_pin = QPushButton("📌 Pin")
        self.btn_pin.setFixedHeight(26)
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip("Keep xGen floating on top of all windows while inspecting")
        self.btn_pin.setStyleSheet(
            "QPushButton { background: #181c24; color: #cbd5e1; border: 1px solid #2a3140; border-radius: 6px; padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 500; text-align: center; }"
            "QPushButton:hover { background: #222834; color: #ffffff; border-color: #8b5cf6; }"
            "QPushButton:checked { background: #3b0764; color: #d8b4fe; border-color: #8b5cf6; font-weight: 600; }"
        )
        self.btn_pin.toggled.connect(self._on_pin_toggled)
        center_layout.addWidget(self.btn_pin)

        layout.addWidget(self.center_container)

        # Space after center buttons to preserve center alignment
        layout.addStretch(1)

        # === 3. Right Section: Test XPath Button & Overflow Menu ===
        self.btn_test_xpath = QPushButton("🧪 Test XPath")
        self.btn_test_xpath.setFixedHeight(26)
        self.btn_test_xpath.setToolTip(
            "Open XPath tester: enter an XPath, match against cached tree or Appium,\n"
            "then Click / Hover / Type on the found element."
        )
        self.btn_test_xpath.setStyleSheet(
            "QPushButton { background: #1e1545; color: #a78bfa; border: 1px solid #4c1d95; border-radius: 6px;"
            "              padding: 2px 12px 3px 12px; font-size: 11px; font-weight: 600; text-align: center; }"
            "QPushButton:hover { background: #2e1065; color: #c4b5fd; border-color: #7c3aed; }"
            "QPushButton:pressed { background: #3b0764; }"
        )
        self.btn_test_xpath.clicked.connect(self._on_test_xpath_clicked)
        layout.addWidget(self.btn_test_xpath)

        # === 4. Right Section: Three-Dot Overflow Menu (Export XML, Guide) ===
        self.btn_overflow = QPushButton("⋯")
        self.btn_overflow.setFixedHeight(26)
        self.btn_overflow.setFixedWidth(28)
        self.btn_overflow.setToolTip("More options (Export XML, XPath Guide)")
        self.btn_overflow.setStyleSheet(
            "QPushButton { background: #181c24; color: #94a3b8; border: 1px solid #2a3140; border-radius: 6px;"
            "              font-size: 14px; font-weight: bold; text-align: center; padding-bottom: 2px; }"
            "QPushButton:hover { background: #222834; color: #ffffff; border-color: #3b82f6; }"
            "QPushButton:pressed { background: #1a202c; }"
        )
        self.btn_overflow.clicked.connect(self._on_overflow_clicked)
        layout.addSpacing(4)
        layout.addWidget(self.btn_overflow)
        layout.addSpacing(2)

        # Compatibility aliases for tests
        self.btn_export_xml = self.btn_overflow
        self.btn_legend = self.btn_overflow

        self.addWidget(container)

    def _on_status_dot_clicked(self) -> None:
        """1-Click Quick Action: Disconnect if active, or Fast-Connect if disconnected."""
        if self._current_state == SessionState.CONNECTING.value:
            return

        if self._current_state == SessionState.CONNECTED.value:
            self.disconnect_requested.emit()
        else:
            # Immediately lock status dot into connecting state to prevent duplicate clicks
            self.set_session_state(SessionState.CONNECTING.value)
            self.fast_connect_requested.emit()

    def set_collapsed_state(self, collapsed: bool) -> None:
        """Toolbar maintains stable geometry independently of tree panel collapse state."""
        pass

    def set_left_width(self, width: int) -> None:
        """Toolbar maintains stable geometry independently of splitter movements."""
        pass

    def set_session_state(self, state_str: str, app_name: str = "") -> None:
        self._current_state = state_str
        self._current_app_name = app_name

        if state_str == SessionState.CONNECTING.value:
            self.btn_status_dot.setEnabled(False)
            self.btn_status_dot.setCursor(Qt.CursorShape.WaitCursor)
        else:
            self.btn_status_dot.setEnabled(True)
            self.btn_status_dot.setCursor(Qt.CursorShape.PointingHandCursor)

        if state_str == SessionState.CONNECTED.value:
            name = app_name or "Desktop Root"
            self.btn_status_dot.setToolTip(f"🟢 Connected to: {name}\nClick to Disconnect")
            self.btn_status_dot.set_colors(
                dot="#10b981",
                bg="#064e3b",
                border="#059669",
                hover_bg="#047857",
                hover_border="#34d399"
            )
        elif state_str == SessionState.CONNECTING.value:
            self.btn_status_dot.setToolTip("🟡 Connecting to Appium...")
            self.btn_status_dot.set_colors(
                dot="#fbbf24",
                bg="#451a03",
                border="#b45309",
                hover_bg="#78350f",
                hover_border="#f59e0b"
            )
        elif state_str == SessionState.LOST.value:
            self.btn_status_dot.setToolTip("⚠️ Session Lost\nClick to Reconnect")
            self.btn_status_dot.set_colors(
                dot="#f87171",
                bg="#450a0a",
                border="#dc2626",
                hover_bg="#5c0f0f",
                hover_border="#ef4444"
            )
        else:
            self.btn_status_dot.setToolTip("🔴 Disconnected\nClick to Quick-Connect to last target")
            self.btn_status_dot.set_colors(
                dot="#ef4444",
                bg="#201314",
                border="#7f1d1d",
                hover_bg="#2d1618",
                hover_border="#ef4444"
            )

    def update_windows_list(self, windows: List[Any], selected_handle: Optional[str] = None) -> None:
        from xgen.utils.window_finder import normalize_handle

        curr_handle = selected_handle
        if (curr_handle is None or curr_handle == "") and self.combo_windows.count() > 0:
            curr_data = self.combo_windows.currentData()
            if curr_data:
                curr_handle = str(curr_data)

        self.combo_windows.blockSignals(True)
        self.combo_windows.clear()

        norm_curr = normalize_handle(curr_handle)

        active_idx = 0
        for idx, w in enumerate(windows):
            if hasattr(w, "display_label"):
                label = w.display_label()
                handle = getattr(w, "handle_hex", "")
                hwnd = getattr(w, "hwnd", 0)
                is_active = False
            else:
                title = getattr(w, "title", "") or f"Window {getattr(w, 'handle', '')}"
                label = f"🪟 {title}"
                handle = getattr(w, "handle", "")
                hwnd = 0
                is_active = getattr(w, "is_active", False)

            self.combo_windows.addItem(label, handle)

            norm_h = normalize_handle(handle) if handle else (hwnd if hwnd else None)
            if norm_curr is not None:
                if norm_h == norm_curr:
                    active_idx = idx
            elif curr_handle is not None and str(curr_handle).lower() in ("", "root"):
                if norm_h is None:
                    active_idx = idx
            elif is_active:
                active_idx = idx

        if windows:
            self.combo_windows.setCurrentIndex(active_idx)
        self.combo_windows.blockSignals(False)

    def set_inspect_active(self, active: bool) -> None:
        self.btn_inspect.blockSignals(True)
        self.btn_inspect.setChecked(active)
        if active:
            self.btn_inspect.setText("🛑 Stop Inspect")
        else:
            self.btn_inspect.setText("🎯 Inspect (F3)")
        self.btn_inspect.blockSignals(False)

    def set_finding_xpath(self, finding: bool) -> None:
        """Update inspect button to show finding/generating state and restore afterwards."""
        self.btn_inspect.blockSignals(True)
        if finding:
            self.btn_inspect.setText("⏳ Finding XPath...")
            self.btn_inspect.setStyleSheet(
                "QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f766e, stop:1 #0d9488); color: #ffffff; border: 1px solid #2dd4bf; border-radius: 6px; padding: 4px 14px; font-weight: 600; font-size: 11px; }"
            )
        else:
            is_active = self.btn_inspect.isChecked()
            self.btn_inspect.setStyleSheet(
                "QPushButton { background: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 6px; padding: 4px 14px; font-weight: 600; font-size: 11px; }"
                "QPushButton:hover { background: #0284c7; color: #ffffff; }"
                "QPushButton:checked { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #dc2626, stop:1 #ef4444); color: #ffffff; border: 1px solid #f87171; }"
                "QPushButton:checked:hover { background: #b91c1c; }"
            )
            if is_active:
                self.btn_inspect.setText("🛑 Stop Inspect")
            else:
                self.btn_inspect.setText("🎯 Inspect (F3)")
        self.btn_inspect.blockSignals(False)

    def set_timed_countdown(self, seconds_left: int) -> None:
        """Update timed capture button during countdown and execution."""
        self.btn_timed.blockSignals(True)
        if seconds_left > 0:
            self.btn_timed.setText(f"⏱ {seconds_left}s...")
            self.btn_timed.setEnabled(False)
            self.btn_timed.setStyleSheet(
                "QPushButton { background: #1e1b4b; color: #a5b4fc; border: 1px solid #6366f1; border-radius: 6px; padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 600; text-align: center; }"
            )
        else:
            self.btn_timed.setText("⏱ Capturing...")
            self.btn_timed.setEnabled(False)
            self.btn_timed.setStyleSheet(
                "QPushButton { background: #312e81; color: #c7d2fe; border: 1px solid #818cf8; border-radius: 6px; padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 600; text-align: center; }"
            )
        self.btn_timed.blockSignals(False)

    def reset_timed_button(self) -> None:
        """Reset timed capture button back to idle state."""
        self.btn_timed.blockSignals(True)
        self.btn_timed.setText("⏱ 5s")
        self.btn_timed.setEnabled(True)
        self.btn_timed.setStyleSheet(
            "QPushButton { background: #181c24; color: #cbd5e1; border: 1px solid #2a3140; border-radius: 6px; padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 500; text-align: center; }"
            "QPushButton:hover { background: #222834; color: #ffffff; border-color: #3b82f6; }"
        )
        self.btn_timed.blockSignals(False)

    def _on_pin_toggled(self, checked: bool) -> None:
        """Reflect the Pin button's checked state in its label, then forward the signal."""
        self.btn_pin.setText("📌 Pinned" if checked else "📌 Pin")
        self.pin_toggled.emit(checked)

    def set_refreshing(self, refreshing: bool) -> None:
        """Update refresh button state, text, style, and enabled flag during tree fetch."""
        self.btn_refresh.blockSignals(True)
        if refreshing:
            self.btn_refresh.setText("🔄 Refreshing...")
            self.btn_refresh.setEnabled(False)
            self.btn_refresh.setToolTip("Fetching fresh UI tree snapshot from target...")
            self.btn_refresh.setStyleSheet(
                "QPushButton { background: #1e2433; color: #60a5fa; border: 1px solid #3b82f6; border-radius: 6px;"
                "              padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 600; text-align: center; }"
            )
        else:
            self.btn_refresh.setText("🔄 Refresh")
            self.btn_refresh.setEnabled(True)
            self.btn_refresh.setToolTip("Fetch fresh UI tree snapshot (Ctrl+R)")
            self.btn_refresh.setStyleSheet(
                "QPushButton { background: #181c24; color: #cbd5e1; border: 1px solid #2a3140; border-radius: 6px;"
                "              padding: 2px 11px 3px 11px; font-size: 11px; font-weight: 500; text-align: center; }"
                "QPushButton:hover { background: #222834; color: #ffffff; border-color: #3b82f6; }"
            )
        self.btn_refresh.blockSignals(False)

    def _on_inspect_clicked(self, checked: bool) -> None:
        self.set_inspect_active(checked)
        self.inspect_toggled.emit(checked)

    def _on_test_xpath_clicked(self) -> None:
        """Emit signal to open the XPath tester popover dialog."""
        self.custom_xpath_test.emit("")

    def _on_window_selected(self, index: int) -> None:
        if index >= 0:
            handle = self.combo_windows.itemData(index)
            if handle is not None:
                self.window_switched.emit(str(handle))

    def _on_overflow_clicked(self) -> None:
        """Show overflow dropdown menu containing Export XML and XPath Guide."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu {"
            "    background-color: #14171e;"
            "    color: #cbd5e1;"
            "    border: 1px solid #28303f;"
            "    border-radius: 6px;"
            "    padding: 4px;"
            "    font-size: 11px;"
            "}"
            "QMenu::item {"
            "    padding: 6px 20px 6px 12px;"
            "    border-radius: 4px;"
            "}"
            "QMenu::item:selected {"
            "    background-color: #2563eb;"
            "    color: #ffffff;"
            "}"
            "QMenu::separator {"
            "    height: 1px;"
            "    background: #28303f;"
            "    margin: 4px 6px;"
            "}"
        )

        act_export = menu.addAction("📤 Export XML")
        act_export.setToolTip("Export current raw XML page source to ./output/ directory")
        act_export.triggered.connect(self.export_xml_requested.emit)

        menu.addSeparator()

        act_guide = menu.addAction("📖 XPath Guide")
        act_guide.setToolTip("Open XPath Guide: Badges, Loc Risk, Stability Scores, and Tiers")
        act_guide.triggered.connect(self.legend_requested.emit)

        # Align popup menu with right edge of overflow button
        pos = self.btn_overflow.mapToGlobal(self.btn_overflow.rect().bottomRight())
        pos.setX(pos.x() - menu.sizeHint().width())
        pos.setY(pos.y() + 2)
        menu.exec(pos)
