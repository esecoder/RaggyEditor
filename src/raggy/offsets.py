"""
offsets.py — reconcile Python character offsets with Scintilla byte offsets.

===============================================================================
WHY THIS EXISTS
===============================================================================
The retrieval engine slices and indexes Python strings, so every `start`/`end`
it reports is a **character** offset.

QScintilla is configured with `setUtf8(True)`, which makes Scintilla's code page
UTF-8 — and in that mode every position it takes or returns (`SCI_SETSEL`,
`SCI_GOTOLINE`, `SCI_SETTARGETSTART`, `SCI_GETSELECTIONSTART`, …) is a **byte**
offset into the UTF-8 encoding.

The two agree only while the document is pure ASCII. Put one em dash, one
accented letter or one emoji anywhere earlier in the file and every subsequent
highlight lands short by the number of extra bytes — silently, with no error:

    text  = "INTRO — with an em dash.\\nERR_CONN_4421 is the code."
    char  = 25                      # engine says the match starts here
    but byte 25 is inside the dash
    → the editor selects  ".\\nERR_CONN_44" instead of "ERR_CONN_4421"

⚠️ This is why the earlier tests did not catch it: they compared the NUMBERS the
engine produced against the NUMBERS Scintilla echoed back, and both were 25. The
numbers match; the *text* does not. Any assertion about offsets has to compare
the text the editor actually selected.

Every crossing of the Python↔Scintilla boundary must go through this class.
"""

from __future__ import annotations

from bisect import bisect_right


class OffsetMap:
    """Bidirectional char ↔ byte mapping for one snapshot of a document.

    Build it from the same text the editor holds. It is immutable: if the
    document changes, build a new one.
    """

    __slots__ = ("text", "_c2b", "byte_length")

    def __init__(self, text: str):
        self.text = text
        # _c2b[i] = byte offset at which character i begins; one extra entry for
        # the end of the string, so to_byte(len) is always valid.
        self._c2b = [0] * (len(text) + 1)
        b = 0
        for i, ch in enumerate(text):
            self._c2b[i] = b
            b += len(ch.encode("utf-8"))
        self._c2b[len(text)] = b
        self.byte_length = b

    @property
    def is_ascii(self) -> bool:
        return self.byte_length == len(self.text)

    def to_byte(self, char_offset: int) -> int:
        """Character offset (Python) -> byte offset (Scintilla)."""
        n = len(self.text)
        if char_offset <= 0:
            return 0
        if char_offset >= n:
            return self.byte_length
        return self._c2b[char_offset]

    def to_char(self, byte_offset: int) -> int:
        """Byte offset (Scintilla) -> character offset (Python)."""
        if byte_offset <= 0:
            return 0
        if byte_offset >= self.byte_length:
            return len(self.text)
        # _c2b is strictly increasing, so this is the character containing the
        # byte, i.e. the last character whose start is <= byte_offset.
        return bisect_right(self._c2b, byte_offset) - 1
