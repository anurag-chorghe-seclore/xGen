"""
Appium XPath Compatibility & Readability Normalization Layer.

Which constructs are rejected depends on the driver on the other end: the
Appium Windows Driver / WinAppDriver engine supports only a narrow XPath 1.0
subset, while the Mac2 driver evaluates XPath server-side with a complete
engine. The per-driver list lives on the DriverDialect (see
xgen/core/driver_dialect.py); the Windows list stays the default so nothing
changes for existing callers.
"""

from __future__ import annotations

import re
from typing import List, Optional

from xgen.core.driver_dialect import DEFAULT_DIALECT, DriverDialect


class AppiumXPathCompatLayer:
    """Validates syntax compatibility and normalizes formatting of XPath selectors."""

    # Disallowed unbounded or unsupported axes/functions in Appium Windows Driver XPath 1.0 engine
    DISALLOWED_PATTERNS: List[re.Pattern] = [
        re.compile(p, re.IGNORECASE) for p in DEFAULT_DIALECT.disallowed_xpath_patterns
    ]

    @classmethod
    def _patterns_for(cls, dialect: Optional[DriverDialect]) -> List[re.Pattern]:
        if dialect is None or dialect is DEFAULT_DIALECT:
            return cls.DISALLOWED_PATTERNS
        return [re.compile(p, re.IGNORECASE) for p in dialect.disallowed_xpath_patterns]

    @classmethod
    def is_appium_compatible(cls, xpath: str, dialect: Optional[DriverDialect] = None) -> bool:
        """
        Check if the given XPath selector is supported by the target driver's
        XPath engine (Appium Windows Driver / WinAppDriver by default).
        """
        if not xpath or not xpath.strip():
            return False

        # Reject unsupported functions or unbounded axes
        for pattern in cls._patterns_for(dialect):
            if pattern.search(xpath):
                return False

        # Must start with // or / or (//
        if not (xpath.startswith("//") or xpath.startswith("/") or xpath.startswith("(//") or xpath.startswith("(/")):
            return False

        return True

    @classmethod
    def normalize_readability(cls, xpath: str) -> str:
        """
        Normalize XPath syntax and predicate spacing for consistent, human-readable formatting.
        """
        if not xpath:
            return ""

        s = xpath.strip()

        # Fix spacing around starts-with and contains commas e.g. starts-with(@Name,'val') -> starts-with(@Name, 'val')
        s = re.sub(r"(starts-with|contains)\((@[a-zA-Z0-9_]+),\s*([^)]+)\)", r"\1(\2, \3)", s)

        # Standardize operator spacing in compound predicates (e.g. and @ClassName -> and @ClassName)
        s = re.sub(r"\s+and\s+", " and ", s)
        s = re.sub(r"\s+or\s+", " or ", s)

        # Standardize outer index parentheses spacing e.g. ( //Window... ) [2] -> (//Window...)[2]
        s = re.sub(r"\(\s*//", "(//", s)
        s = re.sub(r"\s*\)\s*\[(\d+)\]", r")[\1]", s)

        return s
