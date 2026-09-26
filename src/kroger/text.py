"""Name normalisation.

The same product shows up differently across receipts ("a2 Milk(R) Vitamin D
Whole Milk, 59 fl oz" vs "A2 MILK WHOLE 59OZ"). name_norm is the join key used
to collapse those into one items row, so this function needs to be stable --
changing it means re-normalising the whole database.
"""

from __future__ import annotations

import re
import unicodedata

# Registered/trademark markers and other receipt noise.
_MARKS = re.compile(r"[®™©]")

# Unit and packaging tokens that add no identity.
_UNITS = (
    r"fl\s*oz|fluid\s*ounces?|oz|ounces?|lb|lbs|pounds?|g|grams?|kg|"
    r"ml|milliliters?|millilitres?|l|liters?|litres?|"
    r"ct|count|counts|pack|packs|pk|cs|case|cases|box|boxes|bag|bags|"
    r"carton|cartons|tube|tubes|can|cans|bottle|bottles|jar|jars|"
    r"roll|rolls|sheet|sheets|slice|slices|servings?|pieces?|pcs|each|"
    r"dozen|doz|qt|quart|quarts|gal|gallon|gallons|pt|pint|pints"
)

# A size is a number *and* a unit. Stripping the unit word alone leaves the
# number behind ("59 fl oz" -> "59"), which is worse than not matching at all,
# because the leftover digits fragment the key. The number+unit pair has to go
# together.
_SIZE = re.compile(rf"\b\d+(?:[.,]\d+)?\s*(?:{_UNITS})\b", re.IGNORECASE)
_BARE_UNIT = re.compile(rf"\b(?:{_UNITS})\b", re.IGNORECASE)

# Generic qualifiers that should not distinguish one product from another.
_FILLER = re.compile(
    r"\b("
    r"the|of|and|with|for|in|a|an|as|at|from|by|size|upc|item|no|number|"
    r"your|our|new|now|best|better|good|great|value|premium|gourmet|"
    r"select|choice|style|brand|product"
    r")\b",
    re.IGNORECASE,
)

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_REPEAT = re.compile(r"\b(\w+)(?:\s+\1\b)+")


def normalize_name(name: str) -> str:
    """Collapse a product description to a comparable key.

    Lowercases, strips accents and trademark marks, drops size/unit tokens and
    filler words, and collapses whitespace. Digits that are *not* part of a size
    are kept because they are often load-bearing ("a2 milk", "5 hour energy",
    "2% milk").

    This is a candidate-generation heuristic, not an identity function. Receipts
    describe the same product inconsistently ("a2 milk vitamin d whole milk" vs
    "a2 milk whole"), so identity resolution is UPC-first and falls back to
    fuzzy similarity -- see find_or_create_item. Use name_norm as an index, not
    as an equality proof.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _MARKS.sub(" ", s).lower()
    s = _NON_ALNUM.sub(" ", s)
    s = _SIZE.sub(" ", s)
    s = _BARE_UNIT.sub(" ", s)
    s = _FILLER.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    # Collapse immediately repeated words ("whole whole" -> "whole"). Only
    # adjacent runs: non-adjacent duplicates are often meaningful
    # ("chicken chicken broth"), so removing those would merge real products.
    return _REPEAT.sub(r"\1", s).strip()


def token_set(name: str) -> frozenset[str]:
    """Token set for fuzzy containment checks during name -> UPC resolution."""
    return frozenset(normalize_name(name).split())


def similarity(a: str, b: str) -> float:
    """Jaccard similarity over normalized tokens, in [0, 1]."""
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
