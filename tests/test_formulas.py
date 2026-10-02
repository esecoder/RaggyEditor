"""Finding notation in plain text, and rendering it without damaging anything.

⚠️ The theme of these tests is RESTRAINT. A converter that rewrites ordinary prose
into symbols is worse than no converter at all, so most of what is checked here is
what must NOT be matched.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy import formulas  # noqa: E402


class TestDetection(unittest.TestCase):

    def one(self, text):
        found = formulas.find_formulas(text)
        self.assertEqual(len(found), 1, f"expected exactly one in {text!r}: {found}")
        return found[0]

    # ---- what must be found ----------------------------------------------
    def test_a_bare_equation(self):
        f = self.one("E = mc^2")
        self.assertEqual(f.kind, "equation")
        self.assertEqual(f.text, "E = mc^2")

    def test_an_equation_with_operators_on_the_left(self):
        f = self.one("x^2 + y^2 = z^2")
        self.assertEqual(f.text, "x^2 + y^2 = z^2")

    def test_a_dollar_delimited_formula(self):
        f = self.one(r"$\alpha + \beta = \gamma$")
        self.assertEqual(f.kind, "inline")
        self.assertEqual(f.body, r"\alpha + \beta = \gamma")

    def test_a_display_formula(self):
        f = self.one(r"$$\int_0^\infty e^{-x} dx = 1$$")
        self.assertEqual(f.kind, "display")

    def test_backslash_delimited_formula(self):
        f = self.one(r"\(a^2 + b^2\)")
        self.assertEqual(f.kind, "inline")

    def test_a_chemical_equation(self):
        f = self.one("2H2 + O2 -> 2H2O")
        self.assertEqual(f.kind, "chemical")

    def test_a_chemical_equation_with_a_real_arrow(self):
        f = self.one("H2SO4 + 2NaOH → Na2SO4 + 2H2O")
        self.assertEqual(f.kind, "chemical")

    # ---- what must NOT be found ------------------------------------------
    def test_a_sentence_with_an_equals_sign_is_not_a_formula(self):
        self.assertEqual(formulas.find_formulas(
            "The answer = what you get when you add them all up"), [])

    def test_prose_mentioning_greek_letters_is_left_alone(self):
        # ⚠️ Expanding `pi` here would rewrite an English sentence.
        self.assertEqual(formulas.find_formulas("the value of pi is roughly three"), [])

    def test_a_windows_path_is_not_a_formula(self):
        self.assertEqual(formulas.find_formulas(
            r"See the file at C:\Users\me for details"), [])

    def test_a_formula_inside_a_sentence_does_not_swallow_the_sentence(self):
        # ⚠️ Regression: the loose pattern first matched "Given that E = mc^2", the
        # guard rejected the phrase, and `finditer` then resumed AFTER it — losing
        # the real formula. The scan retries one character later instead.
        found = formulas.find_formulas("Given that E = mc^2, the mass grows.")
        self.assertEqual([f.text for f in found], ["E = mc^2"])

    def test_several_formulas_in_one_paragraph(self):
        text = "Energy: E = mc^2 and force: F = ma."
        self.assertEqual([f.text for f in formulas.find_formulas(text)],
                         ["E = mc^2", "F = ma"])

    def test_a_formula_does_not_reach_back_over_a_newline(self):
        # ⚠️ Regression: with `\s` in the patterns, `notes\n\nE = mc^2` came back as
        # ONE formula starting at "notes".
        found = formulas.find_formulas("notes\n\nE = mc^2\n")
        self.assertEqual([f.text for f in found], ["E = mc^2"])

    def test_a_chemical_equation_does_not_reach_back_over_a_newline(self):
        # ⚠️ Regression: the chemistry rule matched `mc^2\n\n2H2 + O2 -> 2H2O`.
        found = formulas.find_formulas("E = mc^2\n\n2H2 + O2 -> 2H2O\n")
        self.assertEqual([f.text for f in found], ["E = mc^2", "2H2 + O2 -> 2H2O"])

    def test_a_bare_formula_without_an_arrow_is_not_chemistry(self):
        # `Ca(OH)2` on its own is indistinguishable from a part number.
        self.assertEqual(formulas.find_formulas("Ca(OH)2"), [])

    # ---- offsets ----------------------------------------------------------
    def test_offsets_are_character_offsets(self):
        # The invariant the whole feature rests on: slice the document by the
        # reported offsets and you get the formula back, in a document that has
        # multi-byte characters before it.
        text = "注意 — E = mc^2 and H2O\n"
        found = formulas.find_formulas(text)
        for f in found:
            self.assertEqual(text[f.start:f.end], f.text,
                             "offsets do not slice the document")


class TestRendering(unittest.TestCase):

    def test_superscript_and_subscript(self):
        # `equation` because that is what turns a lone letter into a variable;
        # the `plain` default must not (see the Greek test below).
        self.assertEqual(formulas.render("x^2", "equation"), "<i>x</i><sup>2</sup>")
        self.assertEqual(formulas.render("x^{2}", "equation"), "<i>x</i><sup>2</sup>")
        self.assertEqual(formulas.render("x^2"), "x<sup>2</sup>")

    def test_subscript(self):
        self.assertEqual(formulas.render("H_2O", "chemical"),
                         "H<sub>2</sub>O")

    def test_indices(self):
        self.assertEqual(formulas.render("x_1", "equation"), "<i>x</i><sub>1</sub>")

    def test_latex_greek(self):
        self.assertEqual(formulas.render(r"\alpha"), "α")

    def test_bare_greek_is_expanded_only_when_the_kind_says_it_is_maths(self):
        self.assertEqual(formulas.render("pi", "equation"), "π")
        self.assertEqual(formulas.render("pi", "inline"), "π")
        # ⚠️ The DEFAULT must change nothing. A caller who forgets the kind must
        # not have their sentence rewritten into symbols.
        self.assertEqual(formulas.render("the pi value"), "the pi value")
        self.assertEqual(formulas.render("pi"), "pi")

    def test_ascii_arrows_become_arrows(self):
        # ⚠️ Regression: escaping ran before this, so `->` had already become
        # `-&gt;` and the arrow silently failed to render.
        self.assertIn("→", formulas.render("2H2 + O2 -> 2H2O", "chemical"))
        self.assertNotIn("&gt;", formulas.render("a -> b", "equation"))

    def test_comparison_operators(self):
        rendered = formulas.render("a <= b >= c != d", "equation")
        for symbol in ("≤", "≥", "≠"):
            self.assertIn(symbol, rendered)

    def test_fraction_is_parenthesised(self):
        # ⚠️ Deliberately not stacked: a rich-text label cannot stack one without a
        # table per fraction, and parentheses are unambiguous.
        self.assertIn("(a)/(b)", formulas.render(r"\frac{a}{b}"))

    def test_square_root(self):
        self.assertIn("√(x)", formulas.render(r"\sqrt{x}"))
        self.assertIn("√(x)", formulas.render("sqrt(x)"))

    def test_square_root_keeps_parentheses_around_an_expression(self):
        # ⚠️ Regression: the symbol table turned `\sqrt` into `√` before the
        # brace rule ran, so `\sqrt{x+1}` rendered as the ambiguous `√x+1`.
        self.assertIn("√(x+1)", formulas.render(r"\sqrt{x+1}"))

    def test_chemical_counts_are_subscripts_and_coefficients_are_not(self):
        rendered = formulas.render("2H2O", "chemical")
        self.assertTrue(rendered.startswith("2"), "the coefficient was subscripted")
        self.assertIn("H<sub>2</sub>O", rendered)

    def test_bracketed_group_count(self):
        self.assertIn("(OH)<sub>2</sub>", formulas.render("Ca(OH)2", "chemical"))

    def test_html_in_the_source_is_escaped(self):
        # A document is not markup: `<b>` typed by a user must render as text and
        # never as a tag it did not ask for.
        rendered = formulas.render("<b>", "equation")
        self.assertNotIn("<b>", rendered)
        self.assertIn("&lt;", rendered)
        self.assertIn("&gt;", rendered)
        # ...and the same with the safe default.
        self.assertEqual(formulas.render("a < b > c"), "a &lt; b &gt; c")

    def test_unknown_notation_survives_verbatim(self):
        # ⚠️ The promise: nothing is "fixed up" by guessing.
        self.assertIn(r"\foobar", formulas.render(r"\foobar"))

    def test_render_never_raises_on_odd_input(self):
        for text in ("", "\\\\", "{", "}", "^", "_", "$", "\\frac{", "a=", "^^^", "___"):
            formulas.render(text, "equation")

    def test_render_formula_themes_the_output(self):
        f = formulas.Formula(0, 1, "E = mc^2", "equation")
        self.assertIn("#000000", formulas.render_formula(f, dark=False))
        self.assertIn("#e8e8e8", formulas.render_formula(f, dark=True))


class TestCaretLookup(unittest.TestCase):

    def test_inside_the_formula(self):
        text = "notes\n\nE = mc^2\n"
        found = formulas.formula_at(text, text.index("mc"))
        self.assertIsNotNone(found)
        self.assertEqual(found.text, "E = mc^2")

    def test_immediately_after_a_paste(self):
        # ⚠️ THE case that makes paste work: after pasting, the caret is at the END
        # of the pasted text, not inside it.
        text = "notes\n\nE = mc^2\n"
        found = formulas.formula_at(text, text.index("E = mc^2") + len("E = mc^2"))
        self.assertIsNotNone(found)
        self.assertEqual(found.text, "E = mc^2")

    def test_nothing_on_plain_prose(self):
        text = "just some words here\n"
        self.assertIsNone(formulas.formula_at(text, 5))

    def test_an_empty_document_is_harmless(self):
        self.assertIsNone(formulas.formula_at("", 0))


if __name__ == "__main__":
    unittest.main()
