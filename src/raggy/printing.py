"""
printing.py — lay a document out for paper the way the editor lays it out.

===============================================================================
WHY NOT JUST QTextDocument
===============================================================================
The first implementation handed the text to `QTextDocument` and let Qt paginate
it. That prints *the text content* — not the document as the editor shows it. It
re-wraps at its own width, ignores the editor's wrap setting and tab width, and
gives no way to keep the two in step. A user who has been reading wrapped lines in
the window gets different line breaks on paper, which makes "line 61" in a printed
copy disagree with line 61 on screen.

So the layout is computed here instead, from the same three facts the editor uses:

    the font            -> how wide a character is
    the wrap setting    -> whether long lines break at all
    the tab width       -> what a tab is worth in columns

⚠️ The width measurement is INJECTED (`measure`) rather than done here. That keeps
this module free of Qt, so the wrapping and paging rules are unit-testable without
a window, a printer or a font — the caller passes `QFontMetrics.horizontalAdvance`
and the real page width. It also means the tests below run on a machine with only
numpy.

⚠️ TABS ARE EXPANDED BEFORE WRAPPING. Wrapping first and expanding later (or
measuring a tab as one character) breaks indentation on every printed line, and
the error compounds with depth.
"""

from __future__ import annotations

import re

DEFAULT_TAB_WIDTH = 8


def expand_tabs(line: str, tab_width: int = DEFAULT_TAB_WIDTH) -> str:
    """Replace tabs with spaces at tab stops, so columns survive on paper."""
    tab_width = max(1, int(tab_width))
    if "\t" not in line:
        return line
    out = []
    column = 0
    for ch in line:
        if ch == "\t":
            pad = tab_width - (column % tab_width)
            out.append(" " * pad)
            column += pad
        else:
            out.append(ch)
            column += 1
    return "".join(out)


def _largest_prefix(text: str, measure, max_width: float) -> int:
    """Longest character count whose width still fits. Always at least 1.

    Used for a single word too long for the page: it has to be cut somewhere, and
    a binary search beats measuring every prefix.
    """
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid]) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return lo


def wrap_line(text: str, measure, max_width: float, wrap: bool = True,
              tab_width: int = DEFAULT_TAB_WIDTH) -> list[str]:
    """Break one logical line into the rows it occupies on the page.

    Returns at least one row, so blank lines stay blank lines.

    ⚠️ Splitting on " " is WRONG here. `"    indented".split(" ")` throws the
    indentation away, so every wrapped line loses its leading whitespace and a
    printed listing comes out flat. Tokens are taken as runs of whitespace and
    runs of non-whitespace instead, so indentation is measured and kept.
    """
    text = expand_tabs(text, tab_width)
    if not wrap or max_width <= 0 or measure(text) <= max_width:
        return [text]

    rows: list[str] = []
    current = ""
    for token in re.findall(r"\S+|\s+", text):
        while True:
            if measure(current + token) <= max_width:
                current += token
                break
            if current:
                rows.append(current.rstrip())      # no trailing spaces on paper
                current = ""
                token = token.lstrip(" ")          # spaces at a break point go
                if not token:
                    break
                continue
            # Nothing on the row and it still does not fit: this token is a single
            # unbreakable run (a long path, a base64 blob). Cut it.
            cut = _largest_prefix(token, measure, max_width)
            rows.append(token[:cut])
            token = token[cut:]
            if not token:
                break
    if current or not rows:
        rows.append(current)
    return rows
    return rows


def rows_for_document(text: str, measure, max_width: float, wrap: bool = True,
                      tab_width: int = DEFAULT_TAB_WIDTH) -> list[str]:
    """Every printed row of the whole document, in order."""
    rows: list[str] = []
    for line in text.split("\n"):
        rows.extend(wrap_line(line, measure, max_width, wrap, tab_width))
    return rows


def paginate(rows: list[str], rows_per_page: int) -> list[list[str]]:
    """Split rows into pages. Always at least one page, even for empty input."""
    per_page = max(1, int(rows_per_page))
    if not rows:
        return [[]]
    return [rows[i:i + per_page] for i in range(0, len(rows), per_page)]


def rows_per_page(page_height: float, line_height: float, footer_height: float = 0.0):
    """How many rows fit, reserving room for a footer. Never less than 1."""
    if line_height <= 0:
        return 1
    usable = page_height - footer_height
    return max(1, int(usable // line_height))


def footer_text(name: str, page: int, total: int) -> str:
    return f"{name}   ·   page {page} of {total}"
