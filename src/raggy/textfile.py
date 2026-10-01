"""
textfile.py — read and write a document without changing it.

===============================================================================
THE BUG THIS EXISTS FOR
===============================================================================
The editor opened every file as UTF-8 with Python's default newline handling and
wrote it back the same way. Two consequences, both silent:

    a Windows file        "a\\r\\nb\\r\\n"
    opened and saved  ->  "a\\nb\\n"          line endings rewritten
    a Latin-1 file        b"caf\\xe9"
    opened and saved  ->  "caf\\xc3\\xa9"      bytes changed, file now UTF-8

Nothing warned. A text editor that quietly rewrites the file it was asked to edit
is worse than one that refuses to open it, so a document now carries its own
format — encoding, BOM, newline style — and saving reproduces exactly that.

⚠️ The editor BUFFER ALWAYS USES "\\n". Scintilla is told the document's EOL mode
for display, but the text handed back is normalised. The original style lives in
the `TextFormat` and is restored on write. Mixing the two is how "\\r" ends up
visible in the middle of a document.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path

UTF8 = "utf-8"
UTF8_BOM = "utf-8-sig"
UTF16_LE = "utf-16-le"
UTF16_BE = "utf-16-be"
LATIN1 = "latin-1"

#: Byte-order marks, longest first so UTF-16 LE/BE are not mistaken for UTF-8.
BOM_TO_ENCODING = (
    (codecs.BOM_UTF16_LE, UTF16_LE),
    (codecs.BOM_UTF16_BE, UTF16_BE),
    (codecs.BOM_UTF8, UTF8_BOM),
)

NEWLINE_NAMES = {"\n": "LF", "\r\n": "CRLF", "\r": "CR"}
NEWLINE_BY_NAME = {v: k for k, v in NEWLINE_NAMES.items()}
#: What to tell Scintilla for each newline style (SC_EOL_CRLF=0, CR=1, LF=2).
EOL_MODE = {"\r\n": 0, "\r": 1, "\n": 2}


@dataclass
class TextFormat:
    """How a document is stored on disk. Reproduced verbatim when writing."""

    encoding: str = UTF8
    newline: str = "\n"
    bom: bytes = b""

    @property
    def newline_name(self) -> str:
        return NEWLINE_NAMES.get(self.newline, repr(self.newline))

    @property
    def is_utf8(self) -> bool:
        return self.encoding in (UTF8, UTF8_BOM)

    def describe(self) -> str:
        return f"{self.encoding}, {self.newline_name}"


DEFAULT_FORMAT = TextFormat()


def detect_newline(data: bytes) -> str:
    """The newline the file predominantly uses. LF when there is no evidence.

    ⚠️ Counted, not "first one wins": a single stray CR in a mostly-LF file must
    not decide the format for the whole document.
    """
    crlf = data.count(b"\r\n")
    cr = data.count(b"\r") - crlf
    lf = data.count(b"\n") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def detect_format(data: bytes) -> TextFormat:
    """BOM first, then strict UTF-8, then Latin-1 as the last resort."""
    for bom, encoding in BOM_TO_ENCODING:
        if data.startswith(bom):
            return TextFormat(encoding, detect_newline(data), bom)

    try:
        data.decode(UTF8)
    except UnicodeDecodeError:
        # ⚠️ Latin-1 cannot fail — every byte sequence is a valid Latin-1 string —
        # so it is the floor. A file that is really Shift-JIS decodes to mojibake
        # rather than raising, which is why `decode` also reports `guessed`.
        return TextFormat(LATIN1, detect_newline(data), b"")
    return TextFormat(UTF8, detect_newline(data), b"")


def decode(data: bytes) -> tuple[str, TextFormat, bool]:
    """bytes -> (editor text, format, guessed).

    The text has "\\r\\n" and "\\r" normalised to "\\n", because that is what the
    editor buffer holds. `guessed` is True when the encoding had to be assumed
    (anything other than the UTF-8 the world mostly uses).
    """
    fmt = detect_format(data)
    try:
        if fmt.encoding in (UTF16_LE, UTF16_BE):
            text = data[len(fmt.bom):].decode(fmt.encoding)
        else:
            text = data.decode(fmt.encoding)      # utf-8-sig strips its own BOM
    except UnicodeDecodeError:
        fmt = TextFormat(LATIN1, fmt.newline, b"")
        text = data.decode(LATIN1)
    return normalise_newlines(text), fmt, not fmt.is_utf8


def normalise_newlines(text: str) -> str:
    """Every newline to "\\n". The buffer's one and only line separator."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def encode(text: str, fmt: TextFormat) -> bytes:
    """Editor text -> bytes in the document's own format."""
    body = normalise_newlines(text)
    if fmt.newline != "\n":
        body = body.replace("\n", fmt.newline)

    # ⚠️ Encode the BOM separately. Encoding with "utf-8-sig" AND prepending
    # codecs.BOM_UTF8 writes two marks, which some readers keep as a stray
    # character at the top of the file.
    if fmt.encoding in (UTF16_LE, UTF16_BE):
        return (fmt.bom or b"") + body.encode(fmt.encoding)
    if fmt.encoding == UTF8_BOM:
        return body.encode(UTF8_BOM)
    return body.encode(fmt.encoding, errors="replace")


def read(path: str | Path) -> tuple[str, TextFormat, bool]:
    return decode(Path(path).read_bytes())


def write(path: str | Path, text: str, fmt: TextFormat) -> None:
    Path(path).write_bytes(encode(text, fmt))
