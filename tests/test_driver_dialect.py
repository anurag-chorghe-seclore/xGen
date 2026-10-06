"""
Tests for the driver-dialect seam (Phase M2: macOS / Appium Mac2 Driver).

The load-bearing test here is test_generated_mac_selectors_resolve_against_mac_source:
xGen's whole value is that a generated XPath works when sent back to the
driver, and the driver evaluates it against its *own* page source. So every
selector is re-evaluated against the very XML it was generated from. A
vocabulary mistake (emitting @Name against a driver that spells it @label)
shows up immediately as a selector that matches nothing — which is exactly
how this would fail in the user's hands, just without needing a Mac.
"""

from __future__ import annotations

import pytest
from lxml import etree

from xgen.core.driver_dialect import (
    DEFAULT_DIALECT,
    MAC2_DIALECT,
    WINDOWS_DIALECT,
    DriverDialectStore,
    dialect_for_platform,
    dialect_from_capabilities,
)
from xgen.core.tree_parser import TreeParser, UINode
from xgen.core.xpath_generator import XPathGenerator, XPathTier


# A Mac2Driver-shaped source tree: XCUIElementType* tags, identifier/label/
# title/value attributes, geometry split across x/y/width/height, and the
# empty-string attributes XCUITest really does emit.
MAC_SOURCE_XML = """<XCUIElementTypeApplication elementType="2" identifier="" label="TextEdit" title="TextEdit" enabled="true" x="0" y="0" width="1440" height="900">
  <XCUIElementTypeWindow elementType="5" identifier="_NS:20" label="" title="Untitled 2" enabled="true" x="100" y="100" width="800" height="600">
    <XCUIElementTypeToolbar elementType="78" identifier="" label="" title="" enabled="true" x="100" y="100" width="800" height="40">
      <XCUIElementTypeButton elementType="9" identifier="saveButton" label="Save" title="" value="" enabled="true" x="120" y="110" width="60" height="20"/>
      <XCUIElementTypeButton elementType="9" identifier="" label="Share" title="" value="" enabled="true" x="200" y="110" width="60" height="20"/>
    </XCUIElementTypeToolbar>
    <XCUIElementTypeTextView elementType="40" identifier="" label="" title="" value="Hello world" enabled="true" x="100" y="140" width="800" height="560"/>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""

WINDOWS_SOURCE_XML = """<AppiumAUT>
  <Window Name="Main Window" AutomationId="win_main" ClassName="MainWnd" BoundingRectangle="[0,0][800,600]">
    <ToolBar Name="Standard" BoundingRectangle="[0,0][800,40]">
      <Button Name="Save" AutomationId="btn_save" ClassName="WPFButton" BoundingRectangle="[10,10][70,30]"/>
    </ToolBar>
  </Window>
</AppiumAUT>"""


@pytest.fixture(autouse=True)
def _reset_active_dialect():
    DriverDialectStore.instance().reset()
    yield
    DriverDialectStore.instance().reset()


def _find(root: UINode, predicate) -> UINode:
    """Depth-first search for the first node matching predicate."""
    stack = [root]
    while stack:
        node = stack.pop()
        if predicate(node):
            return node
        stack.extend(reversed(node.children))
    raise AssertionError("No matching node found in tree")


# --- Dialect selection --------------------------------------------------------


def test_default_dialect_is_windows():
    assert DEFAULT_DIALECT is WINDOWS_DIALECT
    assert DriverDialectStore.instance().active is WINDOWS_DIALECT


@pytest.mark.parametrize(
    "platform_name,expected",
    [
        ("mac", MAC2_DIALECT),
        ("macOS", MAC2_DIALECT),
        ("darwin", MAC2_DIALECT),
        ("Windows", WINDOWS_DIALECT),
        ("win32", WINDOWS_DIALECT),
        ("something-else", WINDOWS_DIALECT),
    ],
)
def test_dialect_for_platform(platform_name, expected):
    assert dialect_for_platform(platform_name) is expected


def test_dialect_from_capabilities_prefers_automation_name():
    # automationName is the precise signal; platformName alone is the fallback.
    assert dialect_from_capabilities({"appium:automationName": "mac2"}) is MAC2_DIALECT
    assert dialect_from_capabilities({"automationName": "Windows"}) is WINDOWS_DIALECT
    assert dialect_from_capabilities({"platformName": "mac"}) is MAC2_DIALECT
    assert dialect_from_capabilities({}) is None
    assert dialect_from_capabilities(None) is None


def test_store_activation_round_trip():
    store = DriverDialectStore.instance()
    store.set_active(MAC2_DIALECT)
    assert store.active is MAC2_DIALECT
    store.reset()
    assert store.active is WINDOWS_DIALECT


# --- Parsing Mac2 source ------------------------------------------------------


def test_mac_attributes_are_not_renamed_to_windows_spellings():
    """
    The Windows alias table must not touch a Mac tree: renaming @label to
    @Name here would make every generated selector miss against the driver.
    """
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)
    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")
    assert "label" in button.attributes
    assert "Name" not in button.attributes
    assert "AutomationId" not in button.attributes


def test_mac_name_resolution_falls_back_label_then_title_then_value():
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)

    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")
    assert (button.name, button.name_attr) == ("Save", "label")

    # Window has an empty label but a real title.
    window = _find(root, lambda n: n.tag == "XCUIElementTypeWindow")
    assert (window.name, window.name_attr) == ("Untitled 2", "title")

    # TextView has neither; its live @value is the last resort.
    text_view = _find(root, lambda n: n.tag == "XCUIElementTypeTextView")
    assert (text_view.name, text_view.name_attr) == ("Hello world", "value")


def test_mac_bounds_come_from_separate_xywh_attributes():
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)
    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")
    rect = button.bounding_rect
    assert rect is not None
    assert (rect.left, rect.top, rect.right, rect.bottom) == (120, 110, 180, 130)
    assert rect.width == 60 and rect.height == 20


def test_mac_identifier_maps_to_automation_id_and_class_name_is_absent():
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)
    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")
    assert button.automation_id == "saveButton"
    assert button.class_name == ""     # no ClassName equivalent on this driver
    assert button.help_text == ""
    assert button.runtime_id == ""     # XCUITest has no UIA-style RuntimeId
    assert button.is_enabled is True
    assert button.is_interactive is True


def test_rect_fallback_accepts_a_combined_rect_string():
    """
    Geometry is the one part of the Mac XML shape not yet confirmed against a
    real /source dump, so the dialect also accepts a combined rect attribute.
    """
    xml = (
        '<XCUIElementTypeApplication><XCUIElementTypeButton label="OK" '
        'rect="{{10, 20}, {30, 40}}"/></XCUIElementTypeApplication>'
    )
    root = TreeParser.parse(xml, dialect=MAC2_DIALECT)
    button = root.children[0]
    assert button.bounding_rect is not None
    assert (button.bounding_rect.left, button.bounding_rect.top) == (10, 20)
    assert (button.bounding_rect.width, button.bounding_rect.height) == (30, 40)


# --- Selector generation ------------------------------------------------------


def _generate(node: UINode):
    return XPathGenerator(localization_enabled=True).generate(node, tree_root=None)


def test_mac_selectors_use_mac_vocabulary_only():
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)
    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")

    xpaths = [c.xpath for c in _generate(button)]
    assert xpaths, "expected at least one candidate"

    joined = " ".join(xpaths)
    for windows_only in ("@Name", "@AutomationId", "@ClassName", "@HelpText"):
        assert windows_only not in joined, f"{windows_only} leaked into a Mac selector"

    assert any("//XCUIElementTypeButton[@label='Save']" == x for x in xpaths)
    assert any("@identifier='saveButton'" in x for x in xpaths)


def test_mac_window_scoped_selector_uses_mac_window_tag():
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)
    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")

    candidates = XPathGenerator().generate(button, tree_root=root)
    ancestor_scoped = [c.xpath for c in candidates if c.tier == XPathTier.T4_ANCESTOR_RELATIVE]
    assert ancestor_scoped, "expected ancestor-scoped candidates"
    assert any(x.startswith("//XCUIElementTypeWindow[") for x in ancestor_scoped)
    assert not any("//Window[" in x for x in ancestor_scoped)


def test_generated_mac_selectors_resolve_against_mac_source():
    """
    Every generated selector must actually select the element it was generated
    for, when evaluated against the driver's own XML. This is the check that
    would have caught a wrong attribute name without access to a Mac.
    """
    root = TreeParser.parse(MAC_SOURCE_XML, dialect=MAC2_DIALECT)
    button = _find(root, lambda n: n.attributes.get("identifier") == "saveButton")

    xml_tree = etree.fromstring(MAC_SOURCE_XML.encode("utf-8"))
    expected = xml_tree.xpath("//XCUIElementTypeButton[@identifier='saveButton']")[0]

    candidates = XPathGenerator().generate(button, tree_root=root)
    assert candidates

    for candidate in candidates:
        matches = xml_tree.xpath(candidate.xpath)
        assert matches, f"selector matched nothing against the source XML: {candidate.xpath}"
        assert expected in matches, (
            f"selector matched the wrong element(s): {candidate.xpath}"
        )


def test_generated_windows_selectors_still_resolve_and_keep_windows_vocabulary():
    """Same round-trip for Windows — the regression guard for the refactor."""
    root = TreeParser.parse(WINDOWS_SOURCE_XML, dialect=WINDOWS_DIALECT)
    button = _find(root, lambda n: n.attributes.get("AutomationId") == "btn_save")

    xml_tree = etree.fromstring(WINDOWS_SOURCE_XML.encode("utf-8"))
    expected = xml_tree.xpath("//Button[@AutomationId='btn_save']")[0]

    candidates = XPathGenerator().generate(button, tree_root=root)
    xpaths = [c.xpath for c in candidates]
    assert any("//Button[@Name='Save']" == x for x in xpaths)
    assert any("@AutomationId='btn_save'" in x for x in xpaths)
    assert not any("XCUIElementType" in x for x in xpaths)

    for candidate in candidates:
        matches = xml_tree.xpath(candidate.xpath)
        assert matches, f"selector matched nothing: {candidate.xpath}"
        assert expected in matches, f"selector matched the wrong element(s): {candidate.xpath}"


def test_hand_built_nodes_still_default_to_the_windows_dialect():
    """
    Nodes constructed directly (as plenty of existing tests and the transient
    capture path do) carry no dialect and must keep behaving as before.
    """
    node = UINode(tag="Button", attributes={"Name": "Submit", "AutomationId": "btn"})
    assert node.driver_dialect is WINDOWS_DIALECT
    assert node.name == "Submit"
    assert node.name_attr == "Name"
    xpaths = [c.xpath for c in _generate(node)]
    assert "//Button[@Name='Submit']" in xpaths


def test_active_dialect_is_used_when_parse_is_not_given_one():
    DriverDialectStore.instance().set_active(MAC2_DIALECT)
    root = TreeParser.parse(MAC_SOURCE_XML)
    assert root.driver_dialect is MAC2_DIALECT
    assert root.tag == "XCUIElementTypeApplication"
