"""
XPath Candidate Generator (xGen v3).
Produces ranked XPath selectors restricted to the Appium Windows Driver-compatible subset,
gated by attribute volatility heuristics and readability normalization.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from xgen.core.appium_compat import AppiumXPathCompatLayer
from xgen.core.stability_scorer import StabilityScorer
from xgen.core.tree_cache import TreeCacheStore
from xgen.core.tree_parser import TreeParser, UINode
from xgen.core.volatility_classifier import (
    NumericContext,
    VolatilityClassifier,
    VolatilityVerdict,
)
from xgen.utils.xpath_escape import escape_xpath_literal

logger = logging.getLogger("xgen.generator")


class XPathTier(Enum):
    T1_TYPE_NAME = 1                 # //Button[@Name='Submit'] (Highest semantic priority)
    T1A_TYPE_NAME_STARTS_WITH = 15   # //Button[starts-with(@Name, 'Submit')]
    T1B_TYPE_NAME_CONTAINS = 16      # //Button[contains(@Name, 'Submit')]
    T2_NAME = 2                      # //*[@Name='Submit']
    T2A_NAME_STARTS_WITH = 21        # //*[starts-with(@Name, 'Submit')]
    T2B_NAME_CONTAINS = 22           # //*[contains(@Name, 'Submit')]
    T3_DIRECT_PARENT_SCOPED = 3      # //TitleBar//Button[@Name='Minimize']
    T4_ANCESTOR_RELATIVE = 4         # //Window[starts-with(@Name, 'App')]//Button[@Name='Submit']
    T5_TYPE_AUTO_ID = 5              # //Button[@AutomationId='btn_submit'] (De-prioritized)
    T6_AUTO_ID = 6                   # //*[@AutomationId='btn_submit'] (De-prioritized)
    T7_COMPOUND_ATTRS = 7            # //Button[@AutomationId='...' and @Name='...']
    T8_TYPE_NAME_CLASS = 8           # //Button[@Name='Submit' and @ClassName='WPFButton']
    T8B_CLASS_TOKEN = 82             # //Group[contains(@ClassName, 'monaco-sash')]
    T9_INDEXED_DISAMBIGUATION = 9    # (//Window[starts-with(@Name, 'App')]//Button[@Name='Submit'])[2]
    T10_ANCESTOR_INDEX = 10          # //*[@AutomationId='win_main']//Button[1]
    T11_ABSOLUTE_PATH = 110          # /AppiumAUT/Window/Pane/Button[1] (diagnostic only)


@dataclass
class VerifyResult:
    """Uniqueness match result filled by XPathVerifier."""
    xpath: str
    match_count: int = 0
    status: str = "zero"       # "unique", "multi", "zero", "error"
    matched_nodes: List[UINode] = field(default_factory=list)

    @property
    def is_unique(self) -> bool:
        return self.status == "unique"


@dataclass
class XPathCandidate:
    """Represents a generated XPath selector and its metadata."""
    xpath: str
    tier: XPathTier
    stability_score: int = 0
    stability_label: str = ""
    localization_risk: bool = False
    is_positional: bool = False
    is_data_dependent: bool = False
    verify_result: Optional[VerifyResult] = None
    is_diagnostic_only: bool = False


class XPathGenerator:
    """
    Generates tiered, Appium-safe candidate XPaths for any UINode.
    Prioritizes human-readable semantic locators over build-volatile AutomationIds.
    """

    KNOWN_GENERIC_CONTAINERS = {
        "View", "ClientView", "WinFrameView", "BrowserView", "NonClientView",
        "TopContainerView", "Intermediate D3D Window", "BrowserFrameViewWin",
        "HwndWrapper", "AdornerDecorator", "Canvas", "Grid", "Border",
        "StackPanel", "DockPanel", "ContentPresenter", "ScrollViewer",
        "Chrome_WidgetWin_0", "Chrome_WidgetWin_1", "Chrome_RenderWidgetHostHWND",
        "Pane", "Group"
    }

    def __init__(self, localization_enabled: bool = True):
        self.localization_enabled = localization_enabled

    def generate(
        self,
        node: UINode,
        tree_root: Optional[UINode] = None,
        include_window_prefix: bool = False
    ) -> List[XPathCandidate]:
        """
        Generate all applicable candidate selectors for node.
        Returns candidates ordered by Tier.
        """
        if not node:
            return []

        candidates: List[XPathCandidate] = []
        root = tree_root or (node.parent if node.parent else node)

        # 1. Tier 1 & Tier 2 — Semantic Name & Type+Name groups (Highest Priority)
        candidates.extend(self._tier4_group(node))
        candidates.extend(self._tier3_group(node))

        # 2. Tier 3 & Tier 4 — Direct Parent & Hierarchical Ancestor Climbing
        candidates.extend(self._tier6_ancestor_climbing(node, root))

        # 3. Tier 5 & Tier 6 — AutomationId (De-prioritized, Volatility gated)
        t2 = self._tier2(node)
        if t2:
            candidates.append(t2)
        t1 = self._tier1(node)
        if t1:
            candidates.append(t1)

        # 4. HelpText Fallback (Accessibility descriptions when Name is absent)
        candidates.extend(self._tier_helptext_fallback(node))

        # 5. Tier 7 & Tier 8 — Compound Attributes & Class Tokens (WPF/Electron)
        candidates.extend(self._tier5_group(node))

        # 6. ClassName Only Fallback (for anonymous elements lacking Name / AutoId)
        candidates.extend(self._tier_classname_only(node))

        # 7. Tier 10 — Ancestor + Positional Index
        candidates.extend(self._tier7_ancestor_index(node, root))

        # 8. Tier 11 — Full Absolute Path (Diagnostic only)
        t8 = self._tier8(node)
        if t8:
            candidates.append(t8)

        # Apply Window Container Prefix if requested
        if include_window_prefix:
            candidates = [self._prepend_window_container(c, node) for c in candidates]

        # Normalize formatting and validate compatibility with the target driver's XPath engine
        dialect = node.driver_dialect
        valid_candidates: List[XPathCandidate] = []
        seen_xpaths = set()
        for c in candidates:
            norm_xpath = AppiumXPathCompatLayer.normalize_readability(c.xpath)
            if norm_xpath and AppiumXPathCompatLayer.is_appium_compatible(norm_xpath, dialect):
                c.xpath = norm_xpath
                if c.xpath not in seen_xpaths:
                    seen_xpaths.add(c.xpath)
                    valid_candidates.append(c)

        # Score all candidates
        for c in valid_candidates:
            score, label, loc_risk, is_pos = StabilityScorer.score(
                c.xpath,
                node,
                localization_enabled=self.localization_enabled,
                is_data_dependent=c.is_data_dependent
            )
            c.stability_score = score
            c.stability_label = label
            c.localization_risk = loc_risk
            c.is_positional = is_pos

        return valid_candidates

    # --- Strategy Generators ---

    def _tier1(self, node: UINode) -> Optional[XPathCandidate]:
        attr_id = node.driver_dialect.attr_automation_id
        auto_id = node.automation_id
        if auto_id and attr_id:
            verdict = VolatilityClassifier.classify_id_volatility(auto_id)
            if verdict != VolatilityVerdict.VOLATILE:
                val = escape_xpath_literal(auto_id)
                return XPathCandidate(
                    xpath=f"//*[@{attr_id}={val}]",
                    tier=XPathTier.T6_AUTO_ID
                )
        return None

    def _tier2(self, node: UINode) -> Optional[XPathCandidate]:
        attr_id = node.driver_dialect.attr_automation_id
        auto_id = node.automation_id
        tag = node.tag
        if auto_id and tag and attr_id:
            verdict = VolatilityClassifier.classify_id_volatility(auto_id)
            if verdict != VolatilityVerdict.VOLATILE:
                val = escape_xpath_literal(auto_id)
                return XPathCandidate(
                    xpath=f"//{tag}[@{attr_id}={val}]",
                    tier=XPathTier.T5_TYPE_AUTO_ID
                )
        return None

    def _tier3_group(self, node: UINode) -> List[XPathCandidate]:
        results: List[XPathCandidate] = []
        name = node.name
        if not name:
            return results
        attr_name = node.name_attr

        val_name = escape_xpath_literal(name)
        results.append(XPathCandidate(
            xpath=f"//*[@{attr_name}={val_name}]",
            tier=XPathTier.T2_NAME,
            localization_risk=True
        ))

        # Safe substring extraction (rejects counters/timestamps)
        stable_sub = VolatilityClassifier.extract_stable_substring(name)
        if stable_sub:
            val_sub = escape_xpath_literal(stable_sub)
            if name.startswith(stable_sub):
                results.append(XPathCandidate(
                    xpath=f"//*[starts-with(@{attr_name}, {val_sub})]",
                    tier=XPathTier.T2A_NAME_STARTS_WITH,
                    localization_risk=True
                ))
            else:
                results.append(XPathCandidate(
                    xpath=f"//*[contains(@{attr_name}, {val_sub})]",
                    tier=XPathTier.T2B_NAME_CONTAINS,
                    localization_risk=True
                ))

        return results

    def _tier4_group(self, node: UINode) -> List[XPathCandidate]:
        results: List[XPathCandidate] = []
        name = node.name
        tag = node.tag
        if not name or not tag:
            return results
        attr_name = node.name_attr

        val_name = escape_xpath_literal(name)
        results.append(XPathCandidate(
            xpath=f"//{tag}[@{attr_name}={val_name}]",
            tier=XPathTier.T1_TYPE_NAME,
            localization_risk=True
        ))

        stable_sub = VolatilityClassifier.extract_stable_substring(name)
        if stable_sub:
            val_sub = escape_xpath_literal(stable_sub)
            if name.startswith(stable_sub):
                results.append(XPathCandidate(
                    xpath=f"//{tag}[starts-with(@{attr_name}, {val_sub})]",
                    tier=XPathTier.T1A_TYPE_NAME_STARTS_WITH,
                    localization_risk=True
                ))
            else:
                results.append(XPathCandidate(
                    xpath=f"//{tag}[contains(@{attr_name}, {val_sub})]",
                    tier=XPathTier.T1B_TYPE_NAME_CONTAINS,
                    localization_risk=True
                ))

        return results

    def _tier_helptext_fallback(self, node: UINode) -> List[XPathCandidate]:
        """Generate HelpText-based selectors when Name is absent (drivers that have one)."""
        results: List[XPathCandidate] = []
        attr_help = node.driver_dialect.attr_help_text
        help_text = node.help_text
        if not attr_help or not help_text or node.name or not node.tag:
            return results

        val_help = escape_xpath_literal(help_text[:60])
        results.append(XPathCandidate(
            xpath=f"//{node.tag}[@{attr_help}={val_help}]",
            tier=XPathTier.T7_COMPOUND_ATTRS,
            localization_risk=True
        ))
        return results

    def _tier_classname_only(self, node: UINode) -> List[XPathCandidate]:
        """Generate class-based selectors for anonymous elements lacking Name/AutomationId."""
        results: List[XPathCandidate] = []
        attr_cls = node.driver_dialect.attr_class_name
        if not attr_cls:
            return results  # e.g. Mac2, where the element type *is* the tag
        if node.name or node.automation_id or not node.class_name or not node.tag:
            return results
        if self.is_structurally_generic(node):
            return results

        cls_name = node.class_name
        val_cls = escape_xpath_literal(cls_name)
        results.append(XPathCandidate(
            xpath=f"//{node.tag}[@{attr_cls}={val_cls}]",
            tier=XPathTier.T8_TYPE_NAME_CLASS
        ))

        tokens = [t for t in cls_name.split() if len(t) >= 4 and t not in ("disabled", "enabled", "active")]
        for tok in tokens[:2]:
            val_tok = escape_xpath_literal(tok)
            results.append(XPathCandidate(
                xpath=f"//{node.tag}[contains(@{attr_cls}, {val_tok})]",
                tier=XPathTier.T8B_CLASS_TOKEN
            ))
        return results

    def _tier5_group(self, node: UINode) -> List[XPathCandidate]:
        results: List[XPathCandidate] = []
        dialect = node.driver_dialect
        attr_id = dialect.attr_automation_id
        attr_cls = dialect.attr_class_name
        name = node.name
        attr_name = node.name_attr
        tag = node.tag
        if not tag:
            return results
        auto_id = node.automation_id
        cls_name = node.class_name

        # Compound: AutomationId + Name
        if auto_id and name and attr_id:
            verdict = VolatilityClassifier.classify_id_volatility(auto_id)
            if verdict != VolatilityVerdict.VOLATILE:
                val_id = escape_xpath_literal(auto_id)
                val_name = escape_xpath_literal(name)
                results.append(XPathCandidate(
                    xpath=f"//{tag}[@{attr_id}={val_id} and @{attr_name}={val_name}]",
                    tier=XPathTier.T7_COMPOUND_ATTRS,
                    localization_risk=True
                ))

        if not attr_cls:
            return results  # driver has no ClassName equivalent (e.g. Mac2)

        # Compound: Name + ClassName
        if name and cls_name and not self.is_structurally_generic(node):
            val_name = escape_xpath_literal(name)
            val_cls = escape_xpath_literal(cls_name)
            results.append(XPathCandidate(
                xpath=f"//{tag}[@{attr_name}={val_name} and @{attr_cls}={val_cls}]",
                tier=XPathTier.T8_TYPE_NAME_CLASS,
                localization_risk=True
            ))

        # Electron / Monaco multi-class tokens (e.g. "monaco-sash horizontal disabled")
        if cls_name and not self.is_structurally_generic(node):
            tokens = [t for t in cls_name.split() if len(t) >= 4 and t not in ("disabled", "enabled", "active")]
            for tok in tokens[:2]:
                val_tok = escape_xpath_literal(tok)
                results.append(XPathCandidate(
                    xpath=f"//{tag}[contains(@{attr_cls}, {val_tok})]",
                    tier=XPathTier.T8B_CLASS_TOKEN
                ))
            val_cls = escape_xpath_literal(cls_name)
            results.append(XPathCandidate(
                xpath=f"//{tag}[@{attr_cls}={val_cls}]",
                tier=XPathTier.T8_TYPE_NAME_CLASS
            ))

        return results

    def is_structurally_generic(self, anc: UINode) -> bool:
        """
        Check if a container node is a non-identifying layout wrapper (curated or structural).
        """
        if not anc:
            return False
        if anc.class_name and anc.class_name in self.KNOWN_GENERIC_CONTAINERS:
            return True
        if (
            not anc.name
            and not anc.automation_id
            and anc.child_count >= 1
            and anc.tag in anc.driver_dialect.layout_tags
            and VolatilityClassifier.classify_id_volatility(anc.class_name or "") != VolatilityVerdict.STATIC
        ):
            return True
        return False

    def _tier6_ancestor_climbing(self, node: UINode, tree_root: UINode) -> List[XPathCandidate]:
        """
        Ancestor climbing: Synthesizes direct parent containers (TitleBar, ToolBar, TabItem),
        meaningful high-level windows, and AutoId anchors while pruning noisy layout wrappers.
        """
        results: List[XPathCandidate] = []
        ancestors = TreeParser.get_ancestors(node)
        if not ancestors:
            return results

        dialect = node.driver_dialect
        attr_id = dialect.attr_automation_id
        attr_cls = dialect.attr_class_name
        window_tag = dialect.window_tag
        tag = node.tag
        auto_id = node.automation_id
        name = node.name
        attr_name = node.name_attr
        cls_name = node.class_name

        # Target child selector patterns
        child_patterns: List[str] = []
        if name:
            val_name = escape_xpath_literal(name)
            child_patterns.append(f"//{tag}[@{attr_name}={val_name}]")
        if auto_id and attr_id and VolatilityClassifier.classify_id_volatility(auto_id) != VolatilityVerdict.VOLATILE:
            val_id = escape_xpath_literal(auto_id)
            child_patterns.append(f"//{tag}[@{attr_id}={val_id}]")
        if attr_cls and cls_name and not self.is_structurally_generic(node):
            first_tok = cls_name.split()[0]
            if len(first_tok) >= 4:
                val_tok = escape_xpath_literal(first_tok)
                child_patterns.append(f"//{tag}[contains(@{attr_cls}, {val_tok})]")

        # 1. Direct Parent semantic container
        parent = node.parent
        if parent and parent.tag in dialect.semantic_parent_tags:
            if parent.name:
                val_pname = escape_xpath_literal(parent.name)
                for child_expr in child_patterns:
                    results.append(XPathCandidate(
                        xpath=f"//{parent.tag}[@{parent.name_attr}={val_pname}]{child_expr}",
                        tier=XPathTier.T3_DIRECT_PARENT_SCOPED,
                        localization_risk=bool(name or parent.name)
                    ))
            for child_expr in child_patterns:
                results.append(XPathCandidate(
                    xpath=f"//{parent.tag}{child_expr}",
                    tier=XPathTier.T3_DIRECT_PARENT_SCOPED,
                    localization_risk=bool(name)
                ))

        # 2. Meaningful Ancestor Filter (Windows, ID containers, named panels)
        meaningful_ancestors: List[UINode] = []
        for anc in ancestors:
            if anc.tag == window_tag or anc.depth <= 2:
                meaningful_ancestors.append(anc)
            elif anc.name and not self.is_structurally_generic(anc) and anc.tag not in dialect.noisy_container_tags:
                meaningful_ancestors.append(anc)
            elif anc.automation_id and VolatilityClassifier.classify_id_volatility(anc.automation_id) != VolatilityVerdict.VOLATILE:
                meaningful_ancestors.append(anc)
            elif anc.tag in dialect.structural_anchor_tags:
                meaningful_ancestors.append(anc)

        for anc in meaningful_ancestors:
            anc_anchors: List[str] = []

            if anc.tag == window_tag:
                anc_auto_id = anc.automation_id
                if anc_auto_id and attr_id and VolatilityClassifier.classify_id_volatility(anc_auto_id) == VolatilityVerdict.STATIC:
                    val_aid = escape_xpath_literal(anc_auto_id)
                    anc_anchors.append(f"//{window_tag}[@{attr_id}={val_aid}]")
                if anc.name:
                    stable_sub = VolatilityClassifier.extract_stable_substring(anc.name)
                    if stable_sub and anc.name.startswith(stable_sub):
                        val_short = escape_xpath_literal(stable_sub)
                        anc_anchors.append(f"//{window_tag}[starts-with(@{anc.name_attr}, {val_short})]")
                    val_aname = escape_xpath_literal(anc.name)
                    anc_anchors.append(f"//{window_tag}[@{anc.name_attr}={val_aname}]")
            elif anc.name:
                val_aname = escape_xpath_literal(anc.name)
                anc_anchors.append(f"//{anc.tag}[@{anc.name_attr}={val_aname}]")

            anc_auto_id = anc.automation_id
            if anc.tag != window_tag and anc_auto_id and attr_id and VolatilityClassifier.classify_id_volatility(anc_auto_id) != VolatilityVerdict.VOLATILE:
                val_aid = escape_xpath_literal(anc_auto_id)
                anc_anchors.append(f"//*[@{attr_id}={val_aid}]")

            if attr_cls and anc.class_name and not self.is_structurally_generic(anc):
                first_tok = anc.class_name.split()[0]
                if len(first_tok) >= 4:
                    val_atok = escape_xpath_literal(first_tok)
                    anc_anchors.append(f"//{anc.tag}[contains(@{attr_cls}, {val_atok})]")

            # Combine meaningful ancestor anchors with child patterns
            for anc_expr in anc_anchors:
                for child_expr in child_patterns:
                    results.append(XPathCandidate(
                        xpath=f"{anc_expr}{child_expr}",
                        tier=XPathTier.T4_ANCESTOR_RELATIVE,
                        localization_risk=bool(name or anc.name)
                    ))

        return results

    def _get_subtree_position(self, node: UINode, anc: UINode) -> int:
        """Count 1-based occurrence index of node's tag among descendants under ancestor."""
        tag = node.tag
        count = 0
        queue = deque([anc])
        while queue:
            curr = queue.popleft()
            if curr is not anc and curr.tag == tag:
                count += 1
                if curr is node:
                    return count
            queue.extend(curr.children)
        return node.child_index

    def _tier7_ancestor_index(self, node: UINode, tree_root: UINode) -> List[XPathCandidate]:
        """Generate ancestor-scoped positional selectors for non-identifiable elements."""
        results: List[XPathCandidate] = []
        ancestors = TreeParser.get_ancestors(node)
        dialect = node.driver_dialect
        attr_id = dialect.attr_automation_id
        tag = node.tag
        is_data_dep = node.is_within_repeating_container()

        meaningful_ancestors = [
            a for a in ancestors
            if a.tag in dialect.positional_anchor_tags
            or (a.name and not self.is_structurally_generic(a) and a.tag not in dialect.noisy_container_tags)
            or (a.automation_id and VolatilityClassifier.classify_id_volatility(a.automation_id) != VolatilityVerdict.VOLATILE)
        ]

        for anc in meaningful_ancestors[:4]:
            anc_anchors: List[str] = []
            if anc.name:
                val_aname = escape_xpath_literal(anc.name)
                anc_anchors.append(f"//{anc.tag}[@{anc.name_attr}={val_aname}]")
            elif anc.automation_id and attr_id and VolatilityClassifier.classify_id_volatility(anc.automation_id) != VolatilityVerdict.VOLATILE:
                val_aid = escape_xpath_literal(anc.automation_id)
                anc_anchors.append(f"//*[@{attr_id}={val_aid}]")
            elif anc.tag in dialect.bare_anchor_tags:
                anc_anchors.append(f"//{anc.tag}")

            pos = self._get_subtree_position(node, anc)
            for anc_expr in anc_anchors:
                results.append(XPathCandidate(
                    xpath=f"{anc_expr}//{tag}[{pos}]",
                    tier=XPathTier.T10_ANCESTOR_INDEX,
                    is_positional=True,
                    is_data_dependent=is_data_dep
                ))

        return results

    def _tier8(self, node: UINode) -> Optional[XPathCandidate]:
        if not node or (not node.parent and node.depth == 0):
            return None

        parts: List[str] = []
        curr: Optional[UINode] = node
        while curr is not None:
            if curr.parent is not None:
                parts.append(f"{curr.tag}[{curr.child_index}]")
            else:
                parts.append(curr.tag)
            curr = curr.parent

        parts.reverse()
        abs_xpath = "/" + "/".join(parts)
        return XPathCandidate(
            xpath=abs_xpath,
            tier=XPathTier.T11_ABSOLUTE_PATH,
            is_diagnostic_only=True
        )

    def _prepend_window_container(self, candidate: XPathCandidate, node: UINode) -> XPathCandidate:
        """Prepend the top-level window selector (//Window[@Name='...']) if not already present."""
        dialect = node.driver_dialect
        window_tag = dialect.window_tag
        if candidate.is_diagnostic_only or candidate.xpath.startswith(f"//{window_tag}"):
            return candidate

        ancestors = TreeParser.get_ancestors(node)
        win_anc = next((a for a in reversed(ancestors) if a.tag == window_tag), None)

        if not win_anc:
            cache = TreeCacheStore.instance().get_active()
            if cache and cache.parsed_root and cache.parsed_root.tag == window_tag:
                win_anc = cache.parsed_root
            elif cache and cache.parsed_root:
                win_anc = next((c for c in cache.parsed_root.children if c.tag == window_tag), None)

        if win_anc:
            if win_anc.name:
                stable_sub = VolatilityClassifier.extract_stable_substring(win_anc.name)
                if stable_sub and win_anc.name.startswith(stable_sub):
                    val = escape_xpath_literal(stable_sub)
                    prefix = f"//{window_tag}[starts-with(@{win_anc.name_attr}, {val})]"
                else:
                    val = escape_xpath_literal(win_anc.name)
                    prefix = f"//{window_tag}[@{win_anc.name_attr}={val}]"
            elif win_anc.automation_id and dialect.attr_automation_id:
                val = escape_xpath_literal(win_anc.automation_id)
                prefix = f"//{window_tag}[@{dialect.attr_automation_id}={val}]"
            else:
                prefix = f"//{window_tag}"

            return dataclasses.replace(candidate, xpath=f"{prefix}{candidate.xpath}")
        return candidate
