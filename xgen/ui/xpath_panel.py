"""
XPath Candidate Inspector Panel (Right Panel).
Renders ranked XPath selectors with uniqueness verification, stability metrics, and copy actions.
"""

from __future__ import annotations

import logging
from typing import List, Optional
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from xgen.core.driver_dialect import active_dialect
from xgen.core.tree_cache import TreeCacheStore
from xgen.core.tree_parser import UINode
from xgen.core.xpath_generator import XPathCandidate, XPathGenerator, XPathTier
from xgen.core.xpath_verifier import XPathVerifier

logger = logging.getLogger("xgen.xpath_panel")


class XPathCard(QFrame):
    """Card widget rendering a single XPath candidate selector, live Appium test, and multi-match drawer."""
    copied = pyqtSignal(str)
    test_requested = pyqtSignal(str, object)   # xpath, card
    click_requested = pyqtSignal(str, object)  # xpath, card
    hover_requested = pyqtSignal(str, object)  # xpath, card
    type_requested = pyqtSignal(str, str, object) # xpath, text, card
    highlight_requested = pyqtSignal(str)      # xpath
    select_node_requested = pyqtSignal(object) # UINode

    def __init__(
        self,
        candidate: XPathCandidate,
        rank: int,
        is_primary: bool = False,
        target_node: Optional[UINode] = None,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.candidate = candidate
        self.rank = rank
        self.is_primary = is_primary
        self.target_node = target_node
        self._matches_drawer: Optional[QWidget] = None
        self._init_ui()

    def _init_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.StyledPanel)
        bg = "#131b2e" if self.is_primary else "#15181f"
        border = "#3b82f6" if self.is_primary else "#222733"
        self.setStyleSheet(
            f"XPathCard {{ background: {bg}; border: 1px solid {border}; border-radius: 8px; }}"
            f"XPathCard:hover {{ border-color: #3b82f6; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 1. Header Badges Row
        badge_row = QHBoxLayout()
        badge_row.setSpacing(5)

        # Rank badge
        lbl_rank = QLabel(f"#{self.rank}" if not self.is_primary else f"★ #{self.rank}")
        if self.is_primary:
            lbl_rank.setStyleSheet("background: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6; border-radius: 4px; padding: 2px 5px; font-weight: bold; font-size: 10px;")
            lbl_rank.setToolTip("Top Recommended Selector")
        else:
            lbl_rank.setStyleSheet("background: #1e222b; color: #94a3b8; border-radius: 4px; padding: 2px 5px; font-weight: bold; font-size: 10px;")
        badge_row.addWidget(lbl_rank)

        # Uniqueness badge (Clickable to expand matches if multiple found)
        res = self.candidate.verify_result
        if res and res.is_unique:
            lbl_unique = QLabel("✅ 1 match")
            lbl_unique.setToolTip("Verified 100% Unique: Exactly 1 matching element in active UI tree")
            lbl_unique.setStyleSheet("background: #064e3b; color: #34d399; border: 1px solid #059669; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;")
            badge_row.addWidget(lbl_unique)
        elif res and res.status == "multi":
            self.btn_multi_toggle = QPushButton(f"⚠️ {res.match_count} matches ▾")
            self.btn_multi_toggle.setToolTip("Matches multiple elements in UI tree. Click to view in tree order.")
            self.btn_multi_toggle.setStyleSheet(
                "QPushButton { background: #451a03; color: #fbbf24; border: 1px solid #b45309; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; }"
                "QPushButton:hover { background: #78350f; color: #ffffff; }"
            )
            self.btn_multi_toggle.clicked.connect(self._toggle_matches_drawer)
            badge_row.addWidget(self.btn_multi_toggle)
        else:
            lbl_unique = QLabel("❌ 0")
            lbl_unique.setToolTip("No elements match this XPath in current tree")
            lbl_unique.setStyleSheet("background: #450a0a; color: #f87171; border: 1px solid #dc2626; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;")
            badge_row.addWidget(lbl_unique)

        # Stability rating pill (compact dot + score)
        score_dot = "🟢" if self.candidate.stability_score >= 75 else ("🟡" if self.candidate.stability_score >= 50 else "🔴")
        lbl_stability = QLabel(f"{score_dot} {self.candidate.stability_score}")
        lbl_stability.setToolTip(f"Stability Score: {self.candidate.stability_score}/100 ({self.candidate.stability_label})")
        lbl_stability.setStyleSheet("background: #14171e; color: #cbd5e1; border: 1px solid #28303f; border-radius: 4px; font-size: 10px; padding: 2px 5px; font-weight: 600;")
        badge_row.addWidget(lbl_stability)

        # Localization risk pill
        if self.candidate.localization_risk:
            lbl_loc = QLabel("🌐 Loc")
            lbl_loc.setToolTip("Localization Risk: Uses human-readable visible text that may change in other OS languages")
            lbl_loc.setStyleSheet("background: #271c19; color: #fb923c; border: 1px solid #7c2d12; padding: 2px 5px; border-radius: 4px; font-size: 10px; font-weight: 500;")
            badge_row.addWidget(lbl_loc)

        if self.candidate.is_positional:
            lbl_fragile = QLabel("⚠️ Pos")
            lbl_fragile.setToolTip("Positional Fragility: Uses occurrence/sibling index that may shift during UI layout updates")
            lbl_fragile.setStyleSheet("background: #3a1515; color: #f87171; border: 1px solid #7f1d1d; padding: 2px 5px; border-radius: 4px; font-size: 10px; font-weight: 500;")
            badge_row.addWidget(lbl_fragile)

        badge_row.addStretch()

        # Action Buttons
        self.btn_test = QPushButton("▶ Test")
        self.btn_test.setToolTip("Test selector against active Appium session (POST /element)")
        self.btn_test.setStyleSheet(
            "QPushButton { background: #0f2b24; color: #2dd4bf; border: 1px solid #134e48; border-radius: 5px; padding: 3px 10px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background: #115e59; color: #ffffff; border-color: #2dd4bf; }"
        )
        self.btn_test.clicked.connect(self._on_test_clicked)
        badge_row.addWidget(self.btn_test)

        self.btn_copy = QPushButton("📋 Copy")
        self.btn_copy.setToolTip("Copy XPath selector to clipboard")
        self.btn_copy.setStyleSheet(
            "QPushButton { background: #1e293b; color: #f1f5f9; border: 1px solid #334155; border-radius: 5px; padding: 3px 12px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background: #2563eb; color: #ffffff; border-color: #3b82f6; }"
        )
        self.btn_copy.clicked.connect(self._copy_xpath)
        badge_row.addWidget(self.btn_copy)

        layout.addLayout(badge_row)

        # 2. Monospace XPath selector text box
        lbl_xpath = QLabel(self.candidate.xpath)
        lbl_xpath.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl_xpath.setWordWrap(True)
        font_size = "12px" if self.is_primary else "11px"
        lbl_xpath.setStyleSheet(
            f"QLabel {{ color: #60a5fa; font-family: 'JetBrains Mono', 'Consolas', 'Courier New', monospace; font-size: {font_size}; background: #090a0d; padding: 8px 10px; border-radius: 6px; border: 1px solid #1c202a; }}"
        )
        layout.addWidget(lbl_xpath)

        # 3. Multi-Match Drawer (Shows ordered elements in UI tree)
        if res and res.status == "multi" and res.matched_nodes:
            self._matches_drawer = QFrame()
            self._matches_drawer.setStyleSheet("QFrame { background: #0d0f14; border: 1px solid #252b38; border-radius: 6px; padding: 6px; }")
            drawer_layout = QVBoxLayout(self._matches_drawer)
            drawer_layout.setContentsMargins(8, 8, 8, 8)
            drawer_layout.setSpacing(6)

            lbl_drawer_hdr = QLabel(f"📍 All {res.match_count} Matches in UI Tree (Ordered Top-to-Bottom):")
            lbl_drawer_hdr.setStyleSheet("color: #fbbf24; font-size: 11px; font-weight: 600; border: none; background: transparent;")
            drawer_layout.addWidget(lbl_drawer_hdr)

            for i, m_node in enumerate(res.matched_nodes, 1):
                m_row = QHBoxLayout()
                m_row.setSpacing(6)

                # Path summary label
                is_curr = (self.target_node is not None and m_node is self.target_node)
                prefix_star = "🎯 " if is_curr else f"#{i} "
                path_str = self._format_node_path(m_node)
                lbl_m = QLabel(f"{prefix_star}{path_str}")
                lbl_color = "#60a5fa" if is_curr else "#cbd5e1"
                lbl_m.setStyleSheet(f"color: {lbl_color}; font-size: 10px; font-weight: {'bold' if is_curr else 'normal'}; border: none; background: transparent;")
                m_row.addWidget(lbl_m, 1)

                # Button to select in tree
                btn_jump = QPushButton("🎯 Select in Tree")
                btn_jump.setToolTip("Navigate and select this element in the Tree View")
                btn_jump.setStyleSheet("QPushButton { background: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 4px; font-size: 10px; padding: 2px 8px; font-weight: 500; } QPushButton:hover { background: #0284c7; color: #fff; }")
                btn_jump.clicked.connect(lambda _, n=m_node: self.select_node_requested.emit(n))
                m_row.addWidget(btn_jump)

                # Button to copy exact indexed selector
                indexed_xp = f"({self.candidate.xpath})[{i}]"
                btn_cp_idx = QPushButton(f"📋 Copy [{i}]")
                btn_cp_idx.setToolTip(f"Copy unique indexed selector: {indexed_xp}")
                btn_cp_idx.setStyleSheet("QPushButton { background: #1e293b; color: #94a3b8; border: 1px solid #334155; border-radius: 4px; font-size: 10px; padding: 2px 8px; font-weight: 500; } QPushButton:hover { background: #059669; color: #fff; border-color: #10b981; }")
                btn_cp_idx.clicked.connect(lambda _, xp=indexed_xp: self._copy_text(xp))
                m_row.addWidget(btn_cp_idx)

                drawer_layout.addLayout(m_row)

            self._matches_drawer.setVisible(False)
            layout.addWidget(self._matches_drawer)

        # 4. Live Test Result / Quick Actions Bar (Hidden by default until tested)
        self.action_bar = QWidget()
        self.action_bar_layout = QHBoxLayout(self.action_bar)
        self.action_bar_layout.setContentsMargins(0, 4, 0, 2)
        self.action_bar_layout.setSpacing(6)

        self.lbl_live_status = QLabel("")
        self.lbl_live_status.setStyleSheet("font-size: 10px; font-weight: 600; padding: 3px 8px; border-radius: 4px;")
        self.action_bar_layout.addWidget(self.lbl_live_status)

        self.action_bar_layout.addStretch()

        self.btn_hover = QPushButton("🎯 Hover")
        self.btn_hover.setToolTip("Move mouse cursor over element via Appium")
        self.btn_hover.setStyleSheet(
            "QPushButton { background: #1e1b4b; color: #a5b4fc; border: 1px solid #4338ca; border-radius: 5px; padding: 3px 10px; font-size: 10px; font-weight: 600; min-height: 18px; }"
            "QPushButton:hover { background: #3730a3; color: #ffffff; border-color: #6366f1; }"
            "QPushButton:pressed { background: #312e81; }"
        )
        self.btn_hover.clicked.connect(self._on_hover_clicked)
        self.action_bar_layout.addWidget(self.btn_hover)

        self.btn_type = QPushButton("⌨️ Type")
        self.btn_type.setToolTip("Type text into element (Edit, ComboBox, Search boxes)")
        self.btn_type.setStyleSheet(
            "QPushButton { background: #0c2d48; color: #38bdf8; border: 1px solid #0284c7; border-radius: 5px; padding: 3px 10px; font-size: 10px; font-weight: 600; min-height: 18px; }"
            "QPushButton:hover { background: #0284c7; color: #ffffff; border-color: #38bdf8; }"
            "QPushButton:pressed { background: #0369a1; }"
        )
        self.btn_type.clicked.connect(self._on_type_clicked)
        self.action_bar_layout.addWidget(self.btn_type)

        self.btn_click = QPushButton("👆 Click")
        self.btn_click.setToolTip("Execute live click on element via active Appium session")
        self.btn_click.setStyleSheet(
            "QPushButton { background: #064e3b; color: #34d399; border: 1px solid #059669; border-radius: 5px; padding: 3px 10px; font-size: 10px; font-weight: 600; min-height: 18px; }"
            "QPushButton:hover { background: #059669; color: #ffffff; border-color: #10b981; }"
            "QPushButton:pressed { background: #047857; }"
        )
        self.btn_click.clicked.connect(self._on_click_clicked)
        self.action_bar_layout.addWidget(self.btn_click)

        self.action_bar.setVisible(False)
        layout.addWidget(self.action_bar)

        # 5. Inline Typing Bar (Expands seamlessly when clicking ⌨️ Type)
        self.type_bar = QWidget()
        self.type_bar_layout = QHBoxLayout(self.type_bar)
        self.type_bar_layout.setContentsMargins(0, 4, 0, 0)
        self.type_bar_layout.setSpacing(6)

        self.input_type_text = QLineEdit()
        self.input_type_text.setPlaceholderText("⌨️ Enter text to send to element...")
        self.input_type_text.setStyleSheet(
            "QLineEdit { background: #090b10; color: #f1f5f9; border: 1px solid #0284c7; border-radius: 5px; padding: 4px 8px; font-size: 11px; font-family: 'Segoe UI', sans-serif; }"
            "QLineEdit:focus { border-color: #38bdf8; background: #0d121c; }"
        )
        self.input_type_text.returnPressed.connect(self._submit_type)
        self.type_bar_layout.addWidget(self.input_type_text, 1)

        self.btn_submit_type = QPushButton("Send Keys ↵")
        self.btn_submit_type.setToolTip("Send keys via Appium (Enter)")
        self.btn_submit_type.setStyleSheet(
            "QPushButton { background: #0369a1; color: #ffffff; border: 1px solid #38bdf8; border-radius: 5px; padding: 4px 10px; font-size: 10px; font-weight: 600; }"
            "QPushButton:hover { background: #0284c7; }"
        )
        self.btn_submit_type.clicked.connect(self._submit_type)
        self.type_bar_layout.addWidget(self.btn_submit_type)

        self.btn_cancel_type = QPushButton("✕")
        self.btn_cancel_type.setToolTip("Cancel typing")
        self.btn_cancel_type.setStyleSheet(
            "QPushButton { background: transparent; color: #64748b; border: none; font-size: 11px; font-weight: bold; padding: 2px 6px; }"
            "QPushButton:hover { color: #f87171; }"
        )
        self.btn_cancel_type.clicked.connect(lambda: self.type_bar.setVisible(False))
        self.type_bar_layout.addWidget(self.btn_cancel_type)

        self.type_bar.setVisible(False)
        layout.addWidget(self.type_bar)

    def _toggle_matches_drawer(self) -> None:
        if self._matches_drawer:
            is_vis = self._matches_drawer.isVisible()
            self._matches_drawer.setVisible(not is_vis)
            arrow = "▴" if not is_vis else "▾"
            count = len(self.candidate.verify_result.matched_nodes) if self.candidate.verify_result else 0
            self.btn_multi_toggle.setText(f"⚠️ Multiple ({count} matches) {arrow}")

    def _format_node_path(self, node: UINode) -> str:
        parts: List[str] = []
        curr: Optional[UINode] = node
        while curr and curr.depth > 0:
            parts.append(curr.display_label())
            curr = curr.parent
        parts.reverse()
        return " > ".join(parts) if parts else node.display_label()

    def _on_test_clicked(self) -> None:
        self.btn_test.setText("⏳ Testing...")
        self.btn_test.setEnabled(False)
        self.btn_test.setStyleSheet("QPushButton { background: #132420; color: #5eead4; border: 1px solid #134e48; border-radius: 5px; padding: 3px 10px; font-size: 11px; }")
        self.test_requested.emit(self.candidate.xpath, self)

    def _on_hover_clicked(self) -> None:
        self.btn_hover.setText("⏳...")
        self.btn_hover.setEnabled(False)
        self.hover_requested.emit(self.candidate.xpath, self)

    def _on_type_clicked(self) -> None:
        is_vis = self.type_bar.isVisible()
        self.type_bar.setVisible(not is_vis)
        if not is_vis:
            self.input_type_text.setFocus()
            self.input_type_text.selectAll()

    def _submit_type(self) -> None:
        text = self.input_type_text.text().strip()
        if not text:
            return
        self.type_bar.setVisible(False)
        self.btn_type.setText("⏳...")
        self.btn_type.setEnabled(False)
        self.type_requested.emit(self.candidate.xpath, text, self)

    def _on_click_clicked(self) -> None:
        self.btn_click.setText("⏳ Clicking...")
        self.btn_click.setEnabled(False)
        self.btn_click.setStyleSheet("QPushButton { background: #132420; color: #5eead4; border: 1px solid #134e48; border-radius: 5px; padding: 3px 10px; font-size: 10px; }")
        self.click_requested.emit(self.candidate.xpath, self)

    def show_test_result(self, success: bool, duration_ms: float, error: str = "") -> None:
        """Update live test feedback badge and re-enable action buttons."""
        self.btn_test.setText("▶ Test")
        self.btn_test.setEnabled(True)
        self.btn_test.setStyleSheet(
            "QPushButton { background: #0f2b24; color: #2dd4bf; border: 1px solid #134e48; border-radius: 5px; padding: 3px 10px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background: #115e59; color: #ffffff; border-color: #2dd4bf; }"
        )
        self.action_bar.setVisible(True)
        if success:
            self.lbl_live_status.setText(f"🟢 Found in {duration_ms:.0f}ms")
            self.lbl_live_status.setStyleSheet("background: #064e3b; color: #34d399; border: 1px solid #059669; border-radius: 4px; padding: 3px 8px; font-size: 10px; font-weight: 600;")
            for btn in (self.btn_hover, self.btn_type, self.btn_click):
                btn.setVisible(True)
                btn.setEnabled(True)
            self.btn_hover.setText("🎯 Hover")
            self.btn_type.setText("⌨️ Type")
            self.btn_click.setText("👆 Click")
            self.btn_hover.setStyleSheet("QPushButton { background: #1e1b4b; color: #a5b4fc; border: 1px solid #4338ca; border-radius: 5px; padding: 3px 10px; font-size: 10px; font-weight: 600; min-height: 18px; } QPushButton:hover { background: #3730a3; color: #fff; }")
            self.btn_type.setStyleSheet("QPushButton { background: #0c2d48; color: #38bdf8; border: 1px solid #0284c7; border-radius: 5px; padding: 3px 10px; font-size: 10px; font-weight: 600; min-height: 18px; } QPushButton:hover { background: #0284c7; color: #fff; }")
            self.btn_click.setStyleSheet("QPushButton { background: #064e3b; color: #34d399; border: 1px solid #059669; border-radius: 5px; padding: 3px 10px; font-size: 10px; font-weight: 600; min-height: 18px; } QPushButton:hover { background: #059669; color: #fff; }")
        else:
            self.lbl_live_status.setText(f"🔴 {error or 'Not found'}")
            self.lbl_live_status.setStyleSheet("background: #450a0a; color: #f87171; border: 1px solid #dc2626; border-radius: 4px; padding: 3px 8px; font-size: 10px; font-weight: 600;")
            for btn in (self.btn_hover, self.btn_type, self.btn_click):
                btn.setVisible(False)
            self.type_bar.setVisible(False)

    def _copy_xpath(self) -> None:
        self._copy_text(self.candidate.xpath)
        self.btn_copy.setText("Copied ✓")
        self.btn_copy.setStyleSheet("QPushButton { background: #059669; color: #ffffff; border: 1px solid #10b981; border-radius: 5px; padding: 3px 12px; font-size: 11px; font-weight: bold; }")
        QTimer.singleShot(1500, self._reset_copy_button)
        self.copied.emit(self.candidate.xpath)

    def _copy_text(self, text: str) -> None:
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(text)

    def _reset_copy_button(self) -> None:
        self.btn_copy.setText("📋 Copy")
        self.btn_copy.setStyleSheet(
            "QPushButton { background: #1e293b; color: #f1f5f9; border: 1px solid #334155; border-radius: 5px; padding: 3px 12px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background: #2563eb; color: #ffffff; border-color: #3b82f6; }"
        )


class XPathPanel(QWidget):
    """Right-hand panel showing ranked XPath alternatives and verification status."""
    test_requested = pyqtSignal(str, object)   # xpath, card
    click_requested = pyqtSignal(str, object)  # xpath, card
    hover_requested = pyqtSignal(str, object)  # xpath, card
    type_requested = pyqtSignal(str, str, object) # xpath, text, card
    node_select_requested = pyqtSignal(object) # UINode

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._current_node: Optional[UINode] = None
        self._tree_root: Optional[UINode] = None
        self._lxml_tree = None
        self._candidates: List[XPathCandidate] = []
        self._cards: List[XPathCard] = []
        self._init_ui()

    def _init_ui(self) -> None:
        self.setStyleSheet("XPathPanel { background: #0c0e12; }")
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(8)

        # 1. Header with Container Prefix Toggle
        header_row = QHBoxLayout()
        header_row.setContentsMargins(2, 2, 2, 2)
        header_row.setSpacing(6)

        self.lbl_header = QLabel("XPath Selectors")
        self.lbl_header.setStyleSheet("color: #f1f5f9; font-weight: 600; font-size: 11px; padding: 2px 4px;")
        header_row.addWidget(self.lbl_header)
        header_row.addStretch()

        self.chk_prefix = QCheckBox("//Window Prefix")
        self.chk_prefix.setToolTip("Include top-level //Window container prefix in generated XPaths (e.g. //Window[@Name='...']//Button)")
        self.chk_prefix.setStyleSheet(
            "QCheckBox { color: #94a3b8; font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 4px; background: #14171e; border: 1px solid #232834; }"
            "QCheckBox:hover { color: #f1f5f9; border-color: #3b82f6; }"
            "QCheckBox:checked { color: #60a5fa; border-color: #2563eb; background: #131b2e; }"
            "QCheckBox::indicator { width: 13px; height: 13px; border-radius: 3px; border: 1px solid #334155; background: #181c24; }"
            "QCheckBox::indicator:checked { background: #2563eb; border-color: #3b82f6; }"
        )
        self.chk_prefix.toggled.connect(self._on_prefix_toggled)
        header_row.addWidget(self.chk_prefix)
        main_layout.addLayout(header_row)

        # 2. Scroll Area containing candidate cards
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: #0c0e12; }")
        self.scroll_area.viewport().setStyleSheet("background: #0c0e12; border: none;")

        self.card_container = QWidget()
        self.card_container.setStyleSheet("background: #0c0e12;")
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(8)
        self.card_layout.addStretch()

        self.scroll_area.setWidget(self.card_container)
        main_layout.addWidget(self.scroll_area)

        # Last, not while the header row is being assembled: addWidget() on a
        # layout that is not yet installed on a widget does NOT reparent, so
        # until main_layout.addLayout(header_row) runs, chk_prefix is still a
        # parentless widget — and setVisible(True) on one of those makes Qt
        # show it as its own top-level window. That stray window is what broke
        # the pin-toggle test.
        self.apply_driver_dialect()

    def populate(
        self,
        node: Optional[UINode],
        tree_root: Optional[UINode] = None,
        lxml_tree=None,
        localization_enabled: bool = True
    ) -> None:
        """Generate, verify, and display candidate selectors."""
        self._current_node = node
        self._tree_root = tree_root
        self._lxml_tree = lxml_tree

        # Clear existing cards
        self._clear_cards()

        if not node:
            self.lbl_header.setText("XPath Selectors")
            lbl_empty = QLabel("🎯 Click or hover any element in Inspect Mode (F3) or select a node in the Tree Panel to generate and test unique XPaths.")
            lbl_empty.setWordWrap(True)
            lbl_empty.setStyleSheet("color: #64748b; font-size: 11px; padding: 24px 16px; background: #15181f; border: 1px dashed #222733; border-radius: 8px; line-height: 1.4;")
            self.card_layout.insertWidget(0, lbl_empty)
            return

        self.lbl_header.setText(f"XPath Selectors for: {node.tag}")

        # Generate candidates
        include_prefix = self.chk_prefix.isChecked()
        gen = XPathGenerator(localization_enabled=localization_enabled)
        raw_candidates = gen.generate(node, tree_root=tree_root, include_window_prefix=include_prefix)

        # Retrieve cached node_map for live node resolution
        cache = TreeCacheStore.instance().get_active()
        node_map = cache.node_map if cache else None

        # Verify against live lxml tree with automatic target node disambiguation
        self._candidates = XPathVerifier.verify_batch(
            raw_candidates,
            lxml_tree,
            target_node=node,
            node_map=node_map
        )

        # Filter to top concise selectors for UI display:
        # Prioritize 1) Unique selectors, 2) Multi-match selectors, 3) Prune dead 0-match selectors unless nothing else exists
        unique_matches = [c for c in self._candidates if c.verify_result and c.verify_result.is_unique and not c.is_diagnostic_only]
        multi_matches = [c for c in self._candidates if c.verify_result and c.verify_result.status == "multi" and not c.is_diagnostic_only]
        zero_matches = [c for c in self._candidates if c.verify_result and c.verify_result.status == "zero" and not c.is_diagnostic_only]

        display_candidates = unique_matches[:6]
        if len(display_candidates) < 5:
            display_candidates.extend(multi_matches[: 5 - len(display_candidates)])
        if len(display_candidates) == 0:
            display_candidates.extend(zero_matches[:3])

        diag = next((c for c in self._candidates if c.is_diagnostic_only), None)
        if diag and diag not in display_candidates:
            display_candidates.append(diag)

        # Render cards
        for rank, candidate in enumerate(display_candidates, start=1):
            is_primary = (rank == 1 and not candidate.is_diagnostic_only and bool(candidate.verify_result and candidate.verify_result.is_unique))
            card = XPathCard(candidate, rank=rank, is_primary=is_primary, target_node=node)
            card.test_requested.connect(self.test_requested.emit)
            card.click_requested.connect(self.click_requested.emit)
            card.hover_requested.connect(self.hover_requested.emit)
            card.type_requested.connect(self.type_requested.emit)
            card.select_node_requested.connect(self.node_select_requested)
            self._cards.append(card)
            self.card_layout.insertWidget(self.card_layout.count() - 1, card)

    def clear(self) -> None:
        self._current_node = None
        self._tree_root = None
        self._lxml_tree = None
        self._candidates.clear()
        self._clear_cards()
        self.lbl_header.setText("XPath Selectors")

    def _clear_cards(self) -> None:
        self._cards.clear()
        while self.card_layout.count() > 1:
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_prefix_toggled(self) -> None:
        if self._current_node:
            self.populate(self._current_node, self._tree_root, self._lxml_tree)

    def apply_driver_dialect(self) -> None:
        """Show the //Window prefix toggle only for drivers it means something for.

        Driven by the active session's dialect rather than by sys.platform:
        xGen running on Windows can drive a remote Mac Appium server, and it
        is the driver on the other end that decides whether a window container
        is a useful anchor. See DriverDialect.supports_window_prefix.
        """
        dialect = active_dialect()
        supported = getattr(dialect, "supports_window_prefix", True)
        # setVisible(True) on a widget with no parent shows it as a top-level
        # window of its own, so never un-hide one that isn't parented yet.
        # Hiding is always safe.
        if supported:
            if self.chk_prefix.parent() is not None:
                self.chk_prefix.setVisible(True)
        else:
            self.chk_prefix.setVisible(False)
        if not supported and self.chk_prefix.isChecked():
            # Clear it rather than leave a hidden control silently padding
            # every generated selector with a prefix that anchors nothing.
            self.chk_prefix.setChecked(False)
