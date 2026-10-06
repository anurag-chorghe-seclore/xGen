"""
Element Bridge.
Maps a clicked native UIAElement to its corresponding UINode in the cached Appium XML tree.
Implements the 4-step matching and live UIA fallback strategy.
"""

from __future__ import annotations

from collections import deque
import logging
from typing import List, NamedTuple, Optional

from xgen.core.driver_dialect import active_dialect
from xgen.core.session_manager import SessionManager
from xgen.core.tree_cache import WindowTreeCache
from xgen.core.tree_parser import TreeParser, UINode
from xgen.core.uia_bridge import UIAElement
from xgen.utils.rect import Rect
from xgen.utils.xpath_escape import escape_xpath_literal

logger = logging.getLogger("xgen.bridge")


class BridgeResult(NamedTuple):
    node: Optional[UINode]
    method: str          # "runtime_id" | "bounding_rect" | "point_containment" | "appium_refind" | "uia_fallback" | "not_found"
    confidence: float    # 0.0 - 1.0 numerical confidence
    competing_candidates: int = 0  # Number of overlapping/plausible candidate nodes
    is_live_fallback: bool = False


class ElementBridge:
    """
    Bridges native OS UIA click events to cached XML nodes.
    """

    def find_node(
        self,
        uia_element: UIAElement,
        cache: Optional[WindowTreeCache],
        session: Optional[SessionManager] = None,
        click_x: int = 0,
        click_y: int = 0,
    ) -> BridgeResult:
        """
        Locate matching UINode in cached XML tree.
        Step 1: Match by exact RuntimeId.
        Step 2: Match by exact BoundingRectangle + ControlType.
        Step 3: Point containment search (innermost leaf containing click point).
        Step 4: Appium driver re-find fallback.
        Step 5: Live UIA synthetic fallback if element is missing from cached XML.
        """
        if not uia_element:
            return BridgeResult(node=None, method="not_found", confidence=0.0)

        if cache and cache.parsed_root:
            root = cache.parsed_root

            # Step 1: RuntimeId match with spatial validation (prevents false matches on reused/virtualized IDs)
            if uia_element.runtime_id:
                node = self._match_by_runtime_id(uia_element.runtime_id, root)
                if node is not None:
                    is_valid = True
                    if uia_element.bounding_rect and node.bounding_rect:
                        if not node.bounding_rect.intersects(uia_element.bounding_rect):
                            if click_x > 0 and click_y > 0:
                                is_valid = node.bounding_rect.contains_point(click_x, click_y)
                            else:
                                is_valid = False

                    if is_valid:
                        node.bridge_confidence = 1.0
                        logger.debug("Bridge: Matched by RuntimeId %s (conf: 1.0)", uia_element.runtime_id)
                        return BridgeResult(node=node, method="runtime_id", confidence=1.0, competing_candidates=0)
                    else:
                        logger.warning(
                            "Bridge: RuntimeId %s matched node '%s' [%s] but bounds %s do not overlap clicked target %s. Rejecting false match.",
                            uia_element.runtime_id, node.name, node.tag, node.bounding_rect, uia_element.bounding_rect
                        )

            # Step 2: BoundingRectangle + ControlType match (with DPI/subpixel tolerance)
            if uia_element.bounding_rect:
                overlapping = self.get_overlapping_nodes(uia_element.bounding_rect, cache)
                competing_count = max(0, len(overlapping) - 1)
                node = self._match_by_bounding_rect(
                    rect=uia_element.bounding_rect,
                    tag=uia_element.control_type,
                    name=uia_element.name,
                    root=root
                )
                if node is not None:
                    conf = max(0.4, 0.90 - (0.05 * competing_count))
                    node.bridge_confidence = conf
                    logger.debug("Bridge: Matched by BoundingRectangle %s (conf: %.2f)", uia_element.bounding_rect, conf)
                    return BridgeResult(node=node, method="bounding_rect", confidence=conf, competing_candidates=competing_count)

            # Step 3: Fuzzy Point-containment hit test (matches deepest leaf at click point)
            px = click_x
            py = click_y
            if px <= 0 and py <= 0 and uia_element.bounding_rect:
                r = uia_element.bounding_rect
                px = r.left + r.width // 2
                py = r.top + r.height // 2

            if px > 0 and py > 0:
                node = TreeParser.find_deepest_at_point(root, px, py, tag=uia_element.control_type)
                if node is None and self._is_ambiguous_leaf_type(uia_element.control_type, root):
                    candidate = TreeParser.find_deepest_at_point(root, px, py, tag="")
                    if candidate and candidate.is_interactive and not self._is_container_tag(candidate):
                        node = candidate

                if node is not None:
                    siblings = len(node.parent.children) if node.parent else 1
                    conf = max(0.4, 0.70 - (0.05 * max(0, siblings - 1)))
                    node.bridge_confidence = conf
                    logger.debug("Bridge: Matched by point containment at (%d, %d) (conf: %.2f)", px, py, conf)
                    return BridgeResult(node=node, method="point_containment", confidence=conf, competing_candidates=max(0, siblings - 1))

            # Step 4: Appium Driver re-find fallback
            if session and session.session_info:
                eid = self._match_by_appium_refind(uia_element, session)
                if eid:
                    rt_id = session.get_element_attribute(eid, "RuntimeId")
                    if rt_id:
                        node = self._match_by_runtime_id(rt_id, root)
                        if node is not None:
                            node.bridge_confidence = 0.60
                            logger.debug("Bridge: Matched via Appium re-find (%s) (conf: 0.60)", eid)
                            return BridgeResult(node=node, method="appium_refind", confidence=0.60, competing_candidates=0)

        # Step 5: Virtualized / Missing from XML snapshot -> Live UIA fallback
        logger.info("Bridge: Element not found in XML snapshot. Using Live UIA fallback (conf: 0.40).")
        synthetic_node = self._create_synthetic_uinode(uia_element)
        synthetic_node.bridge_confidence = 0.40
        return BridgeResult(
            node=synthetic_node,
            method="uia_fallback",
            confidence=0.40,
            competing_candidates=0,
            is_live_fallback=True
        )

    def get_overlapping_nodes(self, rect: Rect, cache: WindowTreeCache) -> List[UINode]:
        """Return all nodes whose bounding rectangle intersects or matches rect."""
        if not cache or not cache.parsed_root or not rect:
            return []
        return self._build_disambiguation_list(rect, cache.parsed_root)

    # --- Private Helpers ---

    # Leaf types whose native hit often lands on an inner glyph/label rather
    # than the control the user meant, so a tag-free re-search is worthwhile.
    _AMBIGUOUS_LEAF_TYPES = {
        "windows": {"Edit", "Text", "Image", "Custom"},
        "mac2": {
            "XCUIElementTypeStaticText", "XCUIElementTypeTextField",
            "XCUIElementTypeImage", "XCUIElementTypeTextView", "XCUIElementTypeAny",
        },
    }

    def _is_ambiguous_leaf_type(self, control_type: str, root: UINode) -> bool:
        key = root.driver_dialect.key
        return control_type in self._AMBIGUOUS_LEAF_TYPES.get(key, set())

    @staticmethod
    def _is_container_tag(node: UINode) -> bool:
        """True for window/layout/root tags that should never win a hit-test re-search."""
        dialect = node.driver_dialect
        return (
            node.tag == dialect.window_tag
            or node.tag in dialect.noisy_container_tags
            or node.tag in dialect.root_tags
        )

    def _match_by_runtime_id(self, rt_id: str, root: UINode) -> Optional[UINode]:
        return TreeParser.find_by_runtime_id(root, rt_id)

    def _match_by_bounding_rect(
        self, rect: Rect, tag: str, name: str, root: UINode
    ) -> Optional[UINode]:
        candidates = TreeParser.find_by_bounding_rect(root, rect, tag)
        if not candidates:
            # Try without tag filter if control type names differ slightly
            candidates = TreeParser.find_by_bounding_rect(root, rect, "")

        if not candidates:
            # Try with fuzzy tolerance (within 4 pixels for DPI / border rounding)
            candidates = self._find_by_fuzzy_rect(root, rect, tag, tolerance=4)

        if not candidates:
            return None

        if len(candidates) == 1:
            return candidates[0]

        # Disambiguate: Exact Name match
        if name:
            for c in candidates:
                if c.name == name:
                    return c

        # Disambiguate: Smallest area (innermost child)
        candidates.sort(key=lambda n: n.bounding_rect.area if n.bounding_rect else 99999999)
        return candidates[0]

    def _find_by_fuzzy_rect(self, root: UINode, rect: Rect, tag: str = "", tolerance: int = 4) -> List[UINode]:
        matches: List[UINode] = []
        queue = deque([root])
        while queue:
            node = queue.popleft()
            if node.bounding_rect:
                nr = node.bounding_rect
                if (abs(nr.left - rect.left) <= tolerance and
                    abs(nr.top - rect.top) <= tolerance and
                    abs(nr.right - rect.right) <= tolerance and
                    abs(nr.bottom - rect.bottom) <= tolerance):
                    if not tag or node.tag == tag:
                        matches.append(node)
            queue.extend(node.children)
        return matches

    def _match_by_appium_refind(self, uia_el: UIAElement, session: SessionManager) -> Optional[str]:
        # Construct temporary single-attribute XPath in the active driver's
        # own vocabulary — this one is sent to the driver, so @AutomationId
        # would simply never match on a Mac session.
        dialect = active_dialect()
        temp_xpath = ""
        if uia_el.automation_id and dialect.attr_automation_id and not uia_el.automation_id.isdigit():
            esc = escape_xpath_literal(uia_el.automation_id)
            temp_xpath = f"//*[@{dialect.attr_automation_id}={esc}]"
        elif uia_el.name and dialect.attr_name:
            esc = escape_xpath_literal(uia_el.name)
            temp_xpath = f"//{uia_el.control_type}[@{dialect.attr_name}={esc}]"

        if temp_xpath:
            return session.find_element_by_xpath(temp_xpath)
        return None

    def _build_disambiguation_list(self, rect: Rect, root: UINode) -> List[UINode]:
        matches: List[UINode] = []
        queue = deque([root])
        while queue:
            node = queue.popleft()
            if node.bounding_rect and node.bounding_rect.intersects(rect):
                matches.append(node)
            queue.extend(node.children)
        return matches

    def _create_synthetic_uinode(self, el: UIAElement) -> UINode:
        """
        Create a detached UINode directly from native element properties.

        Attributes are written in the active driver's own vocabulary, because
        selectors generated from this node are sent to that driver: a Mac
        fallback node carrying @Name/@AutomationId would produce selectors
        that match nothing against Mac2Driver's source.
        """
        dialect = active_dialect()
        attrs = {}

        def _put(attr_name: str, value: str) -> None:
            if attr_name and value:
                attrs[attr_name] = value

        _put(dialect.attr_automation_id, el.automation_id)
        _put(dialect.attr_name, el.name)
        _put(dialect.attr_class_name, el.class_name)
        _put(dialect.attr_enabled, str(el.is_enabled))
        _put(dialect.attr_runtime_id, el.runtime_id)
        _put(dialect.attr_help_text, el.help_text)
        if el.aria_properties:
            attrs["AriaProperties"] = el.aria_properties
        attrs.setdefault("ControlType", el.control_type)

        if el.bounding_rect:
            r = el.bounding_rect
            if dialect.attr_bounding_rect:
                attrs[dialect.attr_bounding_rect] = f"[{r.left},{r.top}][{r.right},{r.bottom}]"
            elif dialect.attr_x:
                attrs[dialect.attr_x] = str(r.left)
                attrs[dialect.attr_y] = str(r.top)
                attrs[dialect.attr_width] = str(r.width)
                attrs[dialect.attr_height] = str(r.height)

        return UINode(
            tag=el.control_type,
            attributes=attrs,
            bounding_rect=el.bounding_rect,
            runtime_id=el.runtime_id,
            is_transient=True,
            dialect=dialect,
        )
