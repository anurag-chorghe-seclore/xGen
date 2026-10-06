"""
DriverDialect — the one description of an Appium driver's own XML vocabulary.

Why this exists: xGen's generated XPaths are sent back to the driver, which
evaluates them against *its own* page source. So a selector is only valid if
its tag and attribute names are spelled the way that driver spells them.
WinAppDriver says `//Button[@Name='Save']`; Appium's Mac2 driver says
`//XCUIElementTypeButton[@label='Save']`. Normalizing Mac attributes into
Windows names at parse time would make the tree look familiar and every
generated selector silently fail against the live app — so instead, parsed
nodes keep their *native* attribute names, and everything that reads or
writes a name goes through the dialect.

Selection is driven by the **session's capabilities, not `sys.platform`** —
xGen running on Windows can point at an Appium server on a Mac (the server
URL is user-configurable), so the local OS says nothing about which driver
is on the other end. The local OS is only ever used as the *default* for a
new session's target platform, and even that is asked of the platform
backend rather than branched on here (see `xgen/platform/backend.py`), so
this module stays free of platform-specific code.

Adding a third driver (appium-xcuitest for iOS, say) means adding one
DIALECTS entry here and nothing else.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Set

from xgen.utils.rect import Rect


@dataclass(frozen=True)
class DriverDialect:
    """Everything xGen needs to know about how one driver names things."""

    key: str                       # "windows" | "mac2"
    display_name: str

    # --- Session capabilities ---
    platform_name: str             # "Windows" | "mac"
    automation_name: str           # "Windows" | "mac2"

    # --- XML attribute names, exactly as this driver spells them ---
    # An empty string means "this driver has no equivalent", and every caller
    # treats that as "no value", so the selector tiers that depend on it
    # simply produce nothing rather than emitting an attribute the driver
    # would never match.
    attr_automation_id: str
    attr_class_name: str
    attr_help_text: str
    attr_enabled: str
    attr_offscreen: str
    attr_runtime_id: str

    # Ordered candidates for "the element's human-readable name". The first
    # non-empty one wins, and the generator emits *that* attribute by name,
    # so a Mac element named only by @title yields //...[@title='...'].
    name_attrs: Sequence[str]

    # --- Tag vocabulary ---
    root_tags: Set[str]
    window_tag: str
    # Containers that meaningfully scope a child selector (//ToolBar//Button[...]).
    semantic_parent_tags: Set[str]
    # Pure layout wrappers that should never anchor a selector.
    layout_tags: Set[str]
    # Ancestors worth anchoring a positional (indexed) selector to.
    positional_anchor_tags: Set[str]
    # Structural chrome worth keeping as an ancestor even when unnamed.
    structural_anchor_tags: Set[str]
    # Structural chrome specific enough to anchor a selector by tag alone
    # (//ToolBar//Button[2]); a strict subset of structural_anchor_tags,
    # since e.g. a bare //Document anchor adds nothing.
    bare_anchor_tags: Set[str]
    # Containers too generic to ever be worth naming in a selector; a
    # narrower set than layout_tags, used when filtering ancestors.
    noisy_container_tags: Set[str]
    # Tags a hit-test should prefer when several elements contain a point.
    interactive_tags: Set[str]
    # Tags that are containers of repeating data (list rows, grid cells):
    # a positional selector inside one of these is data-dependent.
    repeating_container_tags: Set[str]

    # --- Geometry ---
    # Name of a single attribute holding the whole rect (WinAppDriver's
    # "BoundingRectangle"), or "" when the driver splits it into separate
    # x/y/width/height attributes instead.
    attr_bounding_rect: str
    attr_x: str = ""
    attr_y: str = ""
    attr_width: str = ""
    attr_height: str = ""
    # Attributes that may carry a whole rect as a loose string, tried after
    # the two shapes above (Mac2 exposes "frame"/"amRect" as a dictionary;
    # how WebDriverAgentMac flattens that into XML is the one piece of this
    # dialect that still needs confirming against a real /source dump, so
    # all three shapes are accepted rather than betting on one).
    rect_fallback_attrs: Sequence[str] = ()

    # Lower-cased attribute spellings this driver is known to vary between
    # releases, mapped to the canonical spelling (WinAppDriver vs. Appium
    # Windows Driver casing). Applied at parse time, so it must only ever
    # contain renames that are safe to send straight back to this driver.
    attr_aliases: Dict[str, str] = field(default_factory=dict)

    # --- Driver quirks ---
    # XPath constructs this driver's engine rejects (WinAppDriver supports a
    # narrow XPath 1.0 subset; XCUITest's engine is far more complete).
    disallowed_xpath_patterns: Sequence[str] = ()
    # Whether GET /session/{id}/window/handles is meaningful here. Used as a
    # hint only — the heartbeat detects an unsupported endpoint at runtime
    # rather than trusting this flag (see SessionManager._check_heartbeat).
    supports_window_handles: bool = True
    # Whether anchoring a selector to its top-level window container is worth
    # offering. On Windows a Desktop Root session spans every application, so
    # the //Window prefix is what stops a selector matching the same control
    # in a different app. A Mac2 session is already scoped to one application,
    # and its root is XCUIElementTypeApplication rather than a window, so the
    # prefix adds length without adding uniqueness — and adds nothing at all
    # when the app has no XCUIElementTypeWindow ancestor to anchor to, which
    # is why the toggle looked inert there. The UI hides it where this is False.
    supports_window_prefix: bool = True

    # --- Derived helpers ---

    @property
    def attr_name(self) -> str:
        """The primary name attribute (the one used when nothing else matched)."""
        return self.name_attrs[0] if self.name_attrs else ""

    def name_of(self, attributes: Dict[str, str]) -> tuple[str, str]:
        """Return (value, attribute_name) for this element's display name, or ("", "")."""
        for attr in self.name_attrs:
            val = (attributes.get(attr) or "").strip()
            if val:
                return val, attr
        return "", ""

    def parse_bounds(self, attributes: Dict[str, str]) -> Optional[Rect]:
        """Build a Rect from whichever geometry shape this driver emits."""
        # 1. Single combined attribute (WinAppDriver's BoundingRectangle).
        if self.attr_bounding_rect:
            rect = Rect.from_appium_string(attributes.get(self.attr_bounding_rect, ""))
            if rect:
                return rect

        # 2. Separate numeric attributes (x/y/width/height).
        if self.attr_x and self.attr_width:
            try:
                x = float(attributes[self.attr_x])
                y = float(attributes[self.attr_y])
                w = float(attributes[self.attr_width])
                h = float(attributes[self.attr_height])
                return Rect(left=int(x), top=int(y), right=int(x + w), bottom=int(y + h))
            except (KeyError, TypeError, ValueError):
                pass

        # 3. A loose rect-ish string ("{{0, 0}, {100, 50}}", "x=0 y=0 w=100 h=50", ...).
        for attr in self.rect_fallback_attrs:
            raw = attributes.get(attr, "")
            if not raw:
                continue
            nums = re.findall(r"-?\d+(?:\.\d+)?", str(raw))
            if len(nums) >= 4:
                try:
                    x, y, w, h = (float(n) for n in nums[:4])
                    return Rect(left=int(x), top=int(y), right=int(x + w), bottom=int(y + h))
                except (TypeError, ValueError):
                    continue
        return None


# --- WinAppDriver / Appium Windows Driver -------------------------------------
# Exactly the vocabulary xGen has always used; defined explicitly so the
# Windows path goes through the same code as Mac and cannot drift from it.

WINDOWS_DIALECT = DriverDialect(
    key="windows",
    display_name="Windows (Appium Windows Driver / WinAppDriver)",
    platform_name="Windows",
    automation_name="Windows",
    attr_automation_id="AutomationId",
    attr_class_name="ClassName",
    attr_help_text="HelpText",
    attr_enabled="IsEnabled",
    attr_offscreen="IsOffscreen",
    attr_runtime_id="RuntimeId",
    name_attrs=("Name",),
    root_tags={"AppiumAUT", "Page", "Root"},
    window_tag="Window",
    semantic_parent_tags={"TitleBar", "ToolBar", "MenuBar", "TabItem", "Tab", "ListItem", "Header"},
    layout_tags={"Pane", "Group", "Custom", "Border", "Canvas", "View", "Panel"},
    positional_anchor_tags={"Window", "TitleBar", "ToolBar", "TabItem"},
    structural_anchor_tags={"TitleBar", "ToolBar", "Document"},
    bare_anchor_tags={"TitleBar", "ToolBar"},
    noisy_container_tags={"Pane", "Group"},
    interactive_tags={
        "Button", "MenuItem", "TabItem", "CheckBox", "RadioButton",
        "Hyperlink", "ComboBox", "Edit", "ListItem", "TreeItem",
        "HeaderItem", "Slider", "ScrollBar", "Spinner", "ProgressBar",
    },
    repeating_container_tags={
        "List", "ListView", "DataGrid", "Table", "ListBox", "Tree", "TreeView", "ItemsControl",
    },
    attr_bounding_rect="BoundingRectangle",
    attr_aliases={
        "automationid": "AutomationId",
        "automation-id": "AutomationId",
        "classname": "ClassName",
        "class-name": "ClassName",
        "controltype": "ControlType",
        "boundingrectangle": "BoundingRectangle",
        "bounding-rectangle": "BoundingRectangle",
        "isenabled": "IsEnabled",
        "is-enabled": "IsEnabled",
        "isoffscreen": "IsOffscreen",
        "is-offscreen": "IsOffscreen",
        "runtimeid": "RuntimeId",
        "runtime-id": "RuntimeId",
        "name": "Name",
        "helptext": "HelpText",
        "help-text": "HelpText",
        "ariaproperties": "AriaProperties",
        "aria-properties": "AriaProperties",
    },
    disallowed_xpath_patterns=(
        r"(?<!-)following::",
        r"(?<!-)preceding::",
        r"\bancestor::",
        r"\bmatches\(",
        r"\blower-case\(",
        r"\bupper-case\(",
        r"\bends-with\(",
        r"\breplace\(",
    ),
    supports_window_handles=True,
)


# --- Appium Mac2 driver (XCTest / WebDriverAgentMac) --------------------------

_XCUI = "XCUIElementType"

MAC2_DIALECT = DriverDialect(
    key="mac2",
    display_name="macOS (Appium Mac2 Driver)",
    platform_name="mac",
    automation_name="mac2",
    # XCUITest's closest thing to an AutomationId. Stable when the app's
    # developers set accessibility identifiers; absent in many AppKit apps.
    attr_automation_id="identifier",
    # No ClassName equivalent exists — the element type *is* the tag. Empty
    # means every ClassName-based selector tier produces nothing on Mac
    # rather than emitting an attribute the driver cannot match.
    attr_class_name="",
    attr_help_text="",
    attr_enabled="enabled",
    attr_offscreen="",
    # XCUITest has no persistent per-element id like UIA's RuntimeId, so
    # ElementBridge's RuntimeId step simply never matches here and it falls
    # through to bounding-rect / point-containment matching.
    attr_runtime_id="",
    # Deliberately stability-ordered, NOT the driver's own amText order
    # (which starts with @value): @value holds live content — a text field's
    # current text, a slider's position — and makes a brittle selector, so
    # it is the last resort here, after the two static labels.
    name_attrs=("label", "title", "placeholderValue", "value"),
    root_tags={f"{_XCUI}Application", "AppiumAUT"},
    window_tag=f"{_XCUI}Window",
    semantic_parent_tags={
        f"{_XCUI}Toolbar", f"{_XCUI}MenuBar", f"{_XCUI}MenuBarItem", f"{_XCUI}Menu",
        f"{_XCUI}TabGroup", f"{_XCUI}Tab", f"{_XCUI}Sheet", f"{_XCUI}Popover",
        f"{_XCUI}NavigationBar", f"{_XCUI}Cell", f"{_XCUI}Row",
    },
    layout_tags={f"{_XCUI}Group", f"{_XCUI}Other", f"{_XCUI}ScrollArea", f"{_XCUI}SplitGroup", f"{_XCUI}Layout"},
    positional_anchor_tags={f"{_XCUI}Window", f"{_XCUI}Toolbar", f"{_XCUI}TabGroup", f"{_XCUI}MenuBar"},
    structural_anchor_tags={f"{_XCUI}Toolbar", f"{_XCUI}MenuBar"},
    bare_anchor_tags={f"{_XCUI}Toolbar", f"{_XCUI}MenuBar"},
    noisy_container_tags={f"{_XCUI}Group", f"{_XCUI}Other"},
    interactive_tags={
        f"{_XCUI}Button", f"{_XCUI}MenuItem", f"{_XCUI}MenuBarItem", f"{_XCUI}CheckBox",
        f"{_XCUI}RadioButton", f"{_XCUI}PopUpButton", f"{_XCUI}ComboBox", f"{_XCUI}TextField",
        f"{_XCUI}SecureTextField", f"{_XCUI}SearchField", f"{_XCUI}TextView", f"{_XCUI}Link",
        f"{_XCUI}Slider", f"{_XCUI}Stepper", f"{_XCUI}Tab", f"{_XCUI}Cell", f"{_XCUI}Row",
        f"{_XCUI}DisclosureTriangle", f"{_XCUI}Switch", f"{_XCUI}SegmentedControl",
    },
    repeating_container_tags={
        f"{_XCUI}Table", f"{_XCUI}Outline", f"{_XCUI}CollectionView", f"{_XCUI}List", f"{_XCUI}ScrollArea",
    },
    # WebDriverAgentMac serializes geometry per-element; accept every
    # plausible shape rather than betting on one before it has been seen.
    attr_bounding_rect="",
    attr_x="x",
    attr_y="y",
    attr_width="width",
    attr_height="height",
    rect_fallback_attrs=("rect", "frame", "amRect"),
    # XCUITest evaluates XPath with a full engine server-side, so the
    # WinAppDriver-era restrictions don't apply; keep the list empty rather
    # than copying limitations that aren't real here.
    disallowed_xpath_patterns=(),
    supports_window_handles=False,
    supports_window_prefix=False,
)


DIALECTS: Dict[str, DriverDialect] = {
    WINDOWS_DIALECT.key: WINDOWS_DIALECT,
    MAC2_DIALECT.key: MAC2_DIALECT,
}

DEFAULT_DIALECT = WINDOWS_DIALECT


def dialect_for_platform(platform_name: str) -> DriverDialect:
    """Map a platformName capability (or a short name like "mac") to a dialect."""
    key = (platform_name or "").strip().lower()
    if key in ("mac", "macos", "darwin", "mac2"):
        return MAC2_DIALECT
    if key in ("windows", "win", "win32"):
        return WINDOWS_DIALECT
    return DEFAULT_DIALECT


def dialect_from_capabilities(caps: Optional[Dict[str, object]]) -> Optional[DriverDialect]:
    """
    Identify the dialect from a session's capabilities — the authoritative
    source, since it is what the server actually reports back.
    """
    if not caps:
        return None
    automation = str(
        caps.get("appium:automationName") or caps.get("automationName") or ""
    ).strip().lower()
    if automation == "mac2":
        return MAC2_DIALECT
    if automation == "windows":
        return WINDOWS_DIALECT
    platform_name = str(caps.get("platformName") or "").strip()
    if platform_name:
        return dialect_for_platform(platform_name)
    return None


class DriverDialectStore:
    """
    Process-wide "which driver are we talking to right now" holder.

    Mirrors the existing TreeCacheStore singleton idiom rather than threading
    a dialect argument through every call site. SessionManager sets it on
    connect; anything that parses a tree or generates a selector reads it.
    Callers that need to be explicit (tests, and any future multi-session
    work) can pass a dialect directly instead — every consumer accepts one.
    """

    _instance: Optional["DriverDialectStore"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # set_active is called from the GUI thread (SessionManager.connect) and
        # from the session worker thread (once the server confirms which driver
        # answered), while the fetcher thread reads it during TreeParser.parse.
        # Reads are cheap, so a plain lock keeps them from tearing.
        self._lock = threading.RLock()
        self._active: DriverDialect = DEFAULT_DIALECT

    @classmethod
    def instance(cls) -> "DriverDialectStore":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = DriverDialectStore()
        return cls._instance

    @property
    def active(self) -> DriverDialect:
        with self._lock:
            return self._active

    def set_active(self, dialect: DriverDialect) -> None:
        with self._lock:
            self._active = dialect

    def reset(self) -> None:
        with self._lock:
            self._active = DEFAULT_DIALECT


def active_dialect() -> DriverDialect:
    """Shorthand for the current session's dialect."""
    return DriverDialectStore.instance().active
