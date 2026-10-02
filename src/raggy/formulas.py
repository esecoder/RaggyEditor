#!/usr/bin/env python3
"""
formulas.py — find notation in plain text, and render it as rich text.

===============================================================================
WHAT THIS IS, AND WHAT IT IS NOT
===============================================================================
This is NOT a LaTeX engine. It is a recogniser and a *presentational* converter:
it finds notation that is already sitting in a plain-text document and turns it
into HTML that Qt's rich-text engine can lay out — superscripts, subscripts,
Greek letters, operators, arrows, and chemical formulae.

⚠️ WHY NOT A REAL RENDERER. Proper LaTeX means a typesetting engine: matplotlib's
mathtext, KaTeX, or MathJax. Matplotlib is deliberately excluded from the shipped
bundle (it is ~40 MB and this app exists to be small), and KaTeX/MathJax mean
QtWebEngine, which is larger still. So the trade is explicit: cover the notation
people actually paste into notes — `E = mc^2`, `H2O`, `2H2 + O2 → 2H2O`,
`\\alpha`, `x^{2}`, `v = u + at` — with no dependency at all, and pass anything
else through UNCHANGED rather than guessing.

⚠️ THE TEXT IS NEVER MODIFIED. Detection returns character offsets into the
document; rendering happens into a preview. The file on disk keeps what the user
typed, which is the only safe default for an editor.

⚠️ NOTHING IS "FIXED UP". An unknown command (`\\foobar`) survives verbatim, and
text that merely looks mathematical stays untouched unless it really is a formula
by the rules in `find_formulas`. A converter that rewrites prose is worse than no
converter.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

# =============================================================================
# NOTATION TABLES
# =============================================================================
# Longest first wherever one name is a prefix of another, so \to does not eat
# \top and \le does not eat \leq.
LATEX_SYMBOLS = {
    # relations and operators
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠",
    r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡", r"\propto": "∝",
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±", r"\mp": "∓",
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\leftrightarrow": "↔", r"\longrightarrow": "⟶", r"\implies": "⟹",
    r"\infty": "∞", r"\partial": "∂", r"\nabla": "∇", r"\sum": "∑",
    r"\prod": "∏", r"\int": "∫", r"\oint": "∮", r"\sqrt": "√",
    r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\cup": "∪", r"\cap": "∩",
    r"\forall": "∀", r"\exists": "∃", r"\therefore": "∴", r"\degree": "°",
    r"\circ": "∘", r"\perp": "⊥", r"\parallel": "∥", r"\angle": "∠",
    # Greek, lower case
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ",
    r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π", r"\rho": "ρ",
    r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    # Greek, upper case
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ",
    r"\Psi": "Ψ", r"\Omega": "Ω",
    # units and misc
    r"\micro": "µ", r"\ohm": "Ω", r"\AA": "Å",
}

#: ASCII spellings people type when they are not writing LaTeX at all.
PLAIN_SYMBOLS = {
    "->": "→", "-->": "⟶", "=>": "⇒", "<->": "↔", "<=": "≤", ">=": "≥",
    "!=": "≠", "~=": "≈", "+/-": "±",
}

#: Names that are safe to expand in an equation: they are not English words.
GREEK_NAMES = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω", "Delta": "Δ", "Omega": "Ω", "Sigma": "Σ",
    "Lambda": "Λ", "Omega": "Ω",
}

#: A chemical element symbol: one uppercase letter and optionally one lowercase.
ELEMENT = r"[A-Z][a-z]?"

DELIMITED = (
    # (opening, closing, kind)
    ("$$", "$$", "display"),
    (r"\[", r"\]", "display"),
    (r"\(", r"\)", "inline"),
    ("$", "$", "inline"),
)

#: A left-hand side that is a symbol or a short term, then `=`.
#: ⚠️ The character class is spaces and TABS, never `\s`. `\s` matches a NEWLINE,
#: which let a formula grow backwards and swallow the line above it —
#: `"notes\n\nE = mc^2"` came back as one formula starting at "notes".
#: ⚠️ The class includes the operators, or `x^2 + y^2 = z^2` matches only from
#: its second term onwards.
_EQUATION = re.compile(
    r"(?<![\w$])(?P<body>[A-Za-z\\][A-Za-z0-9_^{}()\[\]'′+\-*/. \t]{0,28}?"
    r"[ \t]*=[ \t]*[^=\n:;]{1,60})",
    re.MULTILINE)

#: Chemical equation: element-ish tokens with digits, joined by + and an arrow.
#: ⚠️ Spaces and TABS only, never `\s`. With `\s` this matched straight through a
#: newline and returned `"2\n\n2H2 + O2 -> 2H2O"` — a "chemical equation" that
#: starts halfway through the line above, which is nonsense and hides the real
#: formula from the caret.
_CHEMICAL = re.compile(
    r"(?<![\w])(?P<body>(?:\d*[ \t]*" + ELEMENT + r"\d*[ \t]*|\d+[ \t]*|\+[ \t]*|\(|\)|"
    r"→|⟶|->|[ \t]+)+)(?![\w])")

_CHEM_STRONG = re.compile(r"->|→|⟶")


@dataclass
class Formula:
    """One recognised piece of notation, by CHARACTER offsets into the document."""

    start: int
    end: int
    text: str
    kind: str          # inline | display | equation | chemical

    def __str__(self) -> str:                                       # pragma: no cover
        return f"{self.kind}:{self.text!r}"

    @property
    def body(self) -> str:
        """The notation without its delimiters."""
        for opening, closing, _kind in DELIMITED:
            if (self.kind in ("inline", "display")
                    and self.text.startswith(opening) and self.text.endswith(closing)
                    and len(self.text) > len(opening) + len(closing)):
                return self.text[len(opening):-len(closing)]
        return self.text


# =============================================================================
# CONVERSION
# =============================================================================
_BRACED = r"\{(?P<inner>[^{}]*)\}"


def _strip_braces_thin(text: str) -> str:
    """`x^{2}` and `x^2` both become `x<sup>2</sup>`; `{` is grouping, not output."""
    return text


def _superscripts(text: str) -> str:
    return re.sub(r"\^(?:\{(?P<b>[^{}]*)\}|(?P<s>\S))",
                  lambda m: "<sup>" + (m.group("b") if m.group("b") is not None
                                       else m.group("s")) + "</sup>",
                  text)


def _subscripts(text: str) -> str:
    return re.sub(r"_(?:\{(?P<b>[^{}]*)\}|(?P<s>\S))",
                  lambda m: "<sub>" + (m.group("b") if m.group("b") is not None
                                       else m.group("s")) + "</sub>",
                  text)


def _fracs(text: str) -> str:
    """`\\frac{a}{b}` becomes `(a)/(b)`.

    ⚠️ A real fraction is stacked, and a rich-text label cannot stack one without
    a table per fraction. Parenthesised is honest: it is unambiguous, which a
    plain `a/b` is not once the operands have their own operators.
    """
    pattern = re.compile(r"\\frac\s*\{" r"(?P<num>[^{}]*)" r"\}\s*\{"
                         r"(?P<den>[^{}]*)" r"\}")
    previous = None
    while previous != text:
        previous = text
        text = pattern.sub(lambda m: f"({m.group('num')})/({m.group('den')})", text)
    return text


def _sqrt(text: str) -> str:
    """`\\sqrt{x}` and `sqrt(x)` become `√(x)`."""
    text = re.sub(r"\\sqrt\s*\{(?P<a>[^{}]*)\}", lambda m: f"√({m.group('a')})", text)
    text = re.sub(r"\bsqrt\s*\((?P<a>[^()]*)\)", lambda m: f"√({m.group('a')})", text)
    return text


def _symbols(text: str) -> str:
    for name in sorted(LATEX_SYMBOLS, key=len, reverse=True):
        symbol = LATEX_SYMBOLS[name]
        # A command ends at a non-letter; `\pi x` and `\pi` are both fine, and
        # `\pix` is not `\pi` followed by `x`.
        text = re.sub(re.escape(name) + r"(?![A-Za-z])", symbol, text)
    for name in sorted(PLAIN_SYMBOLS, key=len, reverse=True):
        text = text.replace(name, PLAIN_SYMBOLS[name])
    return text


def _greek_words(text: str, enabled: bool) -> str:
    """Turn bare `lambda`/`pi`/... into Greek symbols.

    ⚠️ ONLY inside an equation, and only whole words. Doing this to prose would
    rewrite "the pi value" in a sentence about pies, which is exactly the kind of
    silent damage this module refuses to do.
    """
    if not enabled:
        return text
    for name in sorted(GREEK_NAMES, key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z]){name}(?![A-Za-z])", GREEK_NAMES[name], text)
    return text


def _italic_variables(text: str) -> str:
    """Italicise single-letter variables: `E = mc^2` -> *E* = *m* *c*^2.

    ⚠️ Single letters only, and never inside a tag we have just produced. A
    multi-letter token is a word (`force`), not a variable.
    """
    parts = re.split(r"(<[^>]+>)", text)
    out = []
    for part in parts:
        if part.startswith("<"):
            out.append(part)
            continue
        out.append(re.sub(r"(?<![A-Za-z0-9])([A-Za-z])(?![A-Za-z0-9])",
                          r"<i>\1</i>", part))
    return "".join(out)


def _collapse_optional_braces(text: str) -> str:
    """`{x}` left over from `\\frac`/`\\sqrt` grouping is grouping, not output."""
    return re.sub(r"\{([^{}]*)\}", r"\1", text)


def render(text: str, kind: str = "plain") -> str:
    """Convert notation to HTML for a rich-text widget.

    ⚠️ THE DEFAULT IS `plain`, WHICH CHANGES NOTHING. `inline`, `display`,
    `equation` and `chemical` all mean "this really is notation" and enable the
    rewriting that follows — italic variables, bare `pi` to π. Calling this with
    the default and a sentence must be a no-op, because that is what a caller does
    by accident.

    Unknown notation is passed through, escaped, exactly as written.
    """
    # ⚠️ ORDER MATTERS THREE TIMES OVER.
    # 1. `\frac` and `\sqrt` FIRST. The symbol table turns `\sqrt` into `√`, and
    #    after that the brace-parsing rule no longer recognises it — `\sqrt{x+1}`
    #    came out as the ambiguous `√x+1` instead of `√(x+1)`.
    # 2. Then the operators, BEFORE escaping: `->` escaped first becomes `-&gt;`,
    #    which matches nothing, and the arrow silently fails to render.
    # 3. Escaping BEFORE any tag is inserted, or the `<sup>` we add would be
    #    escaped too.
    body = _fracs(text)
    body = _sqrt(body)
    body = _symbols(body)
    body = html.escape(body, quote=False)
    body = _superscripts(body)
    body = _subscripts(body)
    body = _collapse_optional_braces(body)
    if kind == "chemical":
        body = _chemical_subscripts(body)
    # ⚠️ Inline math is math: `$\alpha$` deserves the same treatment as a bare
    # `E = mc^2`. Both come from `find_formulas`, where each was recognised as
    # notation — unlike a `plain` call, where nothing was.
    mathy = kind in ("equation", "inline", "display")
    body = _greek_words(body, enabled=mathy)
    if mathy:
        body = _italic_variables(body)
    return body


def _chemical_subscripts(text: str) -> str:
    """`H2O` -> H<sub>2</sub>O, and `2H2 + O2` keeps its leading coefficient.

    ⚠️ Digits AFTER an element symbol are counts and become subscripts. A digit
    BEFORE one is a stoichiometric coefficient and must stay full size, so the
    two cases cannot share a rule.
    """
    # Symbol followed by digits -> subscript the digits.
    text = re.sub(rf"({ELEMENT})(\d+)", r"\1<sub>\2</sub>", text)
    # A closing bracket may be followed by a count, e.g. Ca(OH)2.
    text = re.sub(r"(\))(\d+)", r"\1<sub>\2</sub>", text)
    return text


# =============================================================================
# DETECTION
# =============================================================================
def _delimited_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    for opening, closing, kind in DELIMITED:
        pattern = re.compile(re.escape(opening) + r"(?P<body>.+?)" + re.escape(closing),
                             re.DOTALL)
        for match in pattern.finditer(text):
            if not match.group("body").strip():
                continue
            spans.append((match.start(), match.end(), kind))
    # A `$$...$$` also matches the single-`$` rule around its edges; keep the
    # longest span at each start and drop anything inside a longer one.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept: list[tuple[int, int, str]] = []
    for start, end, kind in spans:
        if any(start >= k_start and end <= k_end for k_start, k_end, _ in kept):
            continue
        if kind == "inline" and text[start:end].startswith("$$"):
            continue
        kept.append((start, end, kind))
    return kept


def _looks_like_equation(body: str) -> bool:
    """Guard rails for the loose `x = y` rule.

    ⚠️ Tuned to under-match. A missed formula is a cosmetic loss; a sentence
    rewritten into symbols is a corrupted document.
    """
    body = body.strip().rstrip(",;.")
    if len(body) > 80 or body.count("=") != 1:
        return False
    left, _, right = body.partition("=")
    left, right = left.strip(), right.strip()
    if not left or not right:
        return False

    # ⚠️ The left side is a SYMBOL EXPRESSION, never a phrase. One token may be
    # anything (`Total`); once there are several, every one of them has to be a
    # single letter, an operator, or a term like `x^2` — so "Given that E = mc^2"
    # is rejected because "Given" and "that" are words.
    parts = left.split()
    if len(parts) > 1:
        for part in parts:
            if not re.fullmatch(r"[A-Za-z0-9_^{}()\[\]'′+\-*/.]+", part):
                return False
            if part.isalpha() and len(part) > 1:
                return False
    if len(left) > 24:
        return False
    # ⚠️ A right side of ordinary WORDS is prose, and one word is the limit.
    # "E = mc^2 and force" is not an equation — it is the first half of a sentence
    # that happens to contain a second `=`. Allowing two multi-letter words let
    # `"Energy: E = mc^2 and force: F = ma."` come back as a single "formula".
    words = [w for w in re.findall(r"[A-Za-z]{3,}", right)]
    if len(words) > 1:
        return False
    return bool(re.search(r"[A-Za-z0-9]", right))


def _trim_rhs(body: str) -> str:
    """Cut the right-hand side at the first ordinary word.

    ⚠️ The pattern is deliberately loose so it does not miss real notation, which
    means `"E = mc^2 and force"` arrives as ONE body: the right side is greedy and
    has no idea where the maths stops. Stopping at the first word of three or more
    letters keeps the expression and drops the sentence that follows it, and the
    cut is a character position in the ORIGINAL string so the reported offsets
    still slice the document exactly.

    `\theta` survives (it is not purely alphabetic — it still has its backslash),
    while `and` does not.
    """
    left, equals, right = body.partition("=")
    if not equals or not right:
        return body
    cut = len(right)
    for token in re.finditer(r"\S+", right):
        core = token.group().strip("+-*/(){}[]^_,")
        if core and re.fullmatch(r"[A-Za-z]{3,}", core):
            cut = token.start()
            break
    trimmed = right[:cut].rstrip()
    if not trimmed:
        return body
    return body[:len(body) - len(right)] + trimmed


def find_formulas(text: str) -> list[Formula]:
    """Every recognised piece of notation, in document order.

    Offsets are CHARACTER offsets, matching `str` indexing — the same convention
    the search engine uses, and for the same reason: the caller converts once.
    """
    found: list[Formula] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start, end):
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    for start, end, kind in _delimited_spans(text):
        if overlaps(start, end):
            continue
        found.append(Formula(start, end, text[start:end], kind))
        claimed.append((start, end))

    for match in _CHEMICAL.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        body = match.group("body")
        # ⚠️ Require an ARROW. `2H2 + O2` on its own is indistinguishable from a
        # part number or a licence plate, and matching those would be noise.
        if not _CHEM_STRONG.search(body):
            continue
        if not re.search(ELEMENT, body):
            continue
        found.append(Formula(match.start(), match.end(), body, "chemical"))
        claimed.append((match.start(), match.end()))

    # ⚠️ A SCAN LOOP, NOT finditer. The pattern is deliberately loose and the
    # guard rejects most of what it finds, but `finditer` resumes AFTER a rejected
    # match — so one rejection could hide a real formula further along inside it.
    # "Given that E = mc^2" first matched with the left side "Given that E", the
    # guard rightly rejected it, and the scan then skipped `E = mc^2` entirely.
    # Retrying one character later finds it, because the pattern cannot start
    # mid-word (`(?<![\w$])`).
    position = 0
    while True:
        match = _EQUATION.search(text, position)
        if match is None:
            break
        # Trailing sentence punctuation is not part of the notation: `F = ma.` at
        # the end of a sentence is the formula `F = ma`. Stripping it here keeps
        # `Formula.text` equal to what the offsets select.
        body = _trim_rhs(match.group("body")).rstrip(",;.")
        start = match.start()
        end = start + len(body)          # the trim moved the end, so recompute it
        if _looks_like_equation(body) and not overlaps(start, end):
            found.append(Formula(start, end, body, "equation"))
            claimed.append((start, end))
            position = end
        else:
            position = start + 1

    found.sort(key=lambda f: f.start)
    return found


def formula_at(text: str, char_offset: int) -> Formula | None:
    """The formula containing (or immediately before) a caret position.

    The "immediately before" case is what makes a paste work: after pasting, the
    caret sits at the end of the pasted text, not inside it.
    """
    best = None
    for formula in find_formulas(text):
        if formula.start <= char_offset <= formula.end:
            return formula
        if formula.end <= char_offset and char_offset - formula.end <= 1:
            if best is None or formula.end > best.end:
                best = formula
    return best


def render_formula(formula: Formula, dark: bool = False) -> str:
    """HTML for a formula, themed for the palette it will be shown on."""
    body = render(formula.body, formula.kind)
    ink = "#e8e8e8" if dark else "#000000"
    if formula.kind == "display":
        return (f'<div style="color:{ink}; font-size:15pt; text-align:center;'
                f' margin:6px 0;">{body}</div>')
    return f'<span style="color:{ink}; font-size:13pt;">{body}</span>'
