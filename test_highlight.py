import random
import re
import time
import unittest
from unittest import mock

import jupytext
import mistune

import highlight


ALLOWED_TYPES = {
    "namespace", "class", "enum", "interface", "struct", "typeParameter", "type", "parameter", "variable",
    "property", "enumMember", "decorator", "function", "method", "macro", "label", "comment", "string",
    "keyword", "number", "regexp", "operator",
}
SPEC_MARKER = re.compile(r"^\s*#\s*%%(%*)\s(.*)$|^\s*#\s*(%%|<codecell>|In\[[0-9 ]*\]:?)\s*$")
GUARD_SECONDS = 5.0
LSP_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def decode(data):
    tokens = []
    line = start = 0
    for index in range(0, len(data), 5):
        delta_line, delta_start, length, kind, modifiers = data[index:index + 5]
        line += delta_line
        start = delta_start if delta_line else start + delta_start
        tokens.append((line, start, length, highlight.TOKEN_TYPES[kind]))
    return tokens


def utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def utf16_slice(text, start, length):
    units = text.encode("utf-16-le", "surrogatepass")
    return units[2 * start:2 * (start + length)].decode("utf-16-le", "surrogatepass")


def tokens_of(text):
    return decode(highlight.semantic_tokens(text))


def pairs(text):
    lines = LSP_LINE_BREAK.split(text)
    return [(utf16_slice(lines[line], start, length), kind) for line, start, length, kind in tokens_of(text)]


def markdown_cell(*lines, tag="[markdown]"):
    return f"# %% {tag}\n" + "".join(("# " + line if line else "#") + "\n" for line in lines)


def assert_wire_valid(test, text):
    data = highlight.semantic_tokens(text)
    test.assertEqual(len(data) % 5, 0)
    lines = LSP_LINE_BREAK.split(text)
    line = start = previous_end = 0
    for index in range(0, len(data), 5):
        delta_line, delta_start, length, kind, modifiers = data[index:index + 5]
        test.assertGreaterEqual(delta_line, 0)
        test.assertGreaterEqual(delta_start, 0)
        test.assertGreater(length, 0)
        test.assertEqual(modifiers, 0)
        test.assertLess(kind, len(highlight.TOKEN_TYPES))
        line += delta_line
        start = delta_start if delta_line else start + delta_start
        test.assertLess(line, len(lines))
        test.assertLessEqual(start + length, utf16_length(lines[line]))
        if delta_line == 0 and index:
            test.assertGreaterEqual(start, previous_end)
        previous_end = start + length
    return decode(data)


class CellTests(unittest.TestCase):
    def test_markdown_and_md_tags_are_tokenized(self):
        for tag in ("[markdown]", "[md]", "Some title [markdown]", "[md] tags=[\"x\"]", "[markdown] {\"a\": 1}"):
            with self.subTest(tag=tag):
                self.assertEqual(pairs(markdown_cell("# Title", tag=tag)), [("# Title", "keyword")])

    def test_code_cell_and_header_have_no_tokens(self):
        text = "# # Header\n# %%\n# # code comment\n# %% [markdown]\n# # Real\n"
        self.assertEqual(tokens_of(text), [(4, 2, 6, "keyword")])

    def test_cell_ends_at_next_marker(self):
        for ender in ("# %%", "# %% [raw]", "# %% title", "#%%% x", "#\t%%\tx"):
            with self.subTest(ender=ender):
                text = markdown_cell("# One") + ender + "\n# # Two\n"
                self.assertEqual(tokens_of(text), [(1, 2, 5, "keyword")])

    def test_bare_marker_ends_cell_and_next_markdown_cell_starts(self):
        text = markdown_cell("# One") + "# %%\n# # Skipped\n" + markdown_cell("# Three")
        self.assertEqual([line for line, *_ in tokens_of(text)], [1, 5])

    def test_text_without_markdown_marker_returns_empty_list(self):
        self.assertEqual(highlight.semantic_tokens(""), [])
        self.assertEqual(highlight.semantic_tokens("x = 1\n# # not\n$y$\n"), [])
        self.assertEqual(highlight.semantic_tokens("x = '[markdown]'\n# # Title\n"), [])

    def test_only_a_marker_returns_empty_list(self):
        self.assertEqual(highlight.semantic_tokens("# %% [markdown]"), [])
        self.assertEqual(highlight.semantic_tokens("# %% [markdown]\n"), [])

    def test_untagged_or_wrong_tags_are_not_markdown(self):
        cases = {
            "no space": "# %%md\n# # Title\n",
            "no space bracket": "# %%[markdown]\n# # Title\n",
            "capital": "# %% [Markdown]\n# # Title\n",
            "capital md": "# %% [MD]\n# # Title\n",
            "raw": "# %% [raw]\n# # Title\n",
            "legacy": "# <markdowncell>\n# # Title\n",
            "docstring": "# %%\n\"\"\"\n# Title\n$x$\n\"\"\"\n",
            "tag after metadata": "# %% key=1 [markdown]\n# # Title\n",
            "markdown word": "# %% markdown\n# # Title\n",
            "script": "import os\n\"\"\"\n# Title\n$x$\n\"\"\"\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertEqual(tokens_of(text), [])

    def test_raw_tag_beats_md_tag_like_jupytext(self):
        self.assertEqual(tokens_of("# %% [raw] [md]\n# # Title\n"), [])
        self.assertEqual(pairs("# %% [markdown] [raw]\n# # Title\n"), [("# Title", "keyword")])

    def test_marker_text_gets_no_tokens(self):
        self.assertEqual(tokens_of("# %% [markdown] **bold** $x$ `c`\n# plain\n"), [])

    def test_marker_inside_open_triple_quoted_string_is_not_a_marker(self):
        text = "# %% [markdown]\n\"\"\"\n# One\n# %% [raw]\n# Two\n\"\"\"\n# %%\n# # Out\n"
        self.assertEqual(pairs(text), [("# One", "keyword"), ("# %% [raw]", "keyword"), ("# Two", "keyword")])

    def test_marker_inside_open_string_of_code_cell_is_not_a_marker(self):
        text = "# %%\nx = '''\n# %% [markdown]\n# # Title\n'''\n# %% [markdown]\n# # Real\n"
        self.assertEqual([line for line, *_ in tokens_of(text)], [6])

    def test_marker_inside_closed_fence_is_not_a_marker(self):
        text = markdown_cell("```", "# %% x", "# %% [raw]", "```", "# After")
        self.assertEqual([kind for _, kind in pairs(text)], ["property"] * 4 + ["keyword"])

    def test_marker_inside_unclosed_fence_still_ends_the_cell(self):
        text = markdown_cell("```", "text") + "# %% [raw]\n# # Skipped\n"
        self.assertEqual([(line, kind) for line, _, _, kind in tokens_of(text)], [(1, "property"), (2, "property")])

    def test_marker_inside_closed_fence_in_raw_cell_is_not_a_marker(self):
        text = "# %% [raw]\n# ```\n# %% [markdown]\n# ```\n# %% [markdown]\n# # Real\n"
        self.assertEqual([line for line, *_ in tokens_of(text)], [5])

    def test_indented_marker_and_alternative_markers(self):
        text = "    # %% [markdown]\n# # Not indented\n"
        self.assertEqual(pairs(text), [("# Not indented", "keyword")])
        text = markdown_cell("# One") + "# In[3]:\n# # Skipped\n"
        self.assertEqual(len(tokens_of(text)), 1)


class BodyTests(unittest.TestCase):
    def test_comment_prefix_is_removed(self):
        text = "# %% [markdown]\n# # Title\n#\n#plain *a*\n"
        self.assertEqual(tokens_of(text), [(1, 2, 7, "keyword"), (3, 7, 3, "parameter")])

    def test_bare_hash_line_is_a_blank_line(self):
        text = "# %% [markdown]\n# $$ a\n#\n# b $$\n"
        self.assertEqual(pairs(text), [])

    def test_triple_double_quoted_body(self):
        text = "# %% [markdown]\n\"\"\"\n# Title\n*a* $x$\n\"\"\"\n"
        self.assertEqual([(line, start, kind) for line, start, _, kind in tokens_of(text)],
                         [(2, 0, "keyword"), (3, 0, "parameter"), (3, 4, "operator"), (3, 5, "variable"),
                          (3, 6, "operator")])

    def test_triple_single_and_raw_prefixes(self):
        for opening, closing in (("'''", "'''"), ('r"""', '"""'), ("R'''", "'''"), ("r'''", "'''"), ('R"""', '"""')):
            with self.subTest(opening=opening):
                text = f"# %% [markdown]\n{opening}\n**b**\n{closing}\n"
                self.assertEqual(pairs(text), [("**b**", "enumMember")])

    def test_quotes_themselves_get_no_tokens(self):
        text = "# %% [markdown]\n\"\"\"**b** and\n*i*\"\"\"\n"
        self.assertEqual(tokens_of(text), [(1, 3, 5, "enumMember"), (2, 0, 3, "parameter")])
        text = "# %% [markdown]\n  r'''*i*'''  \n"
        self.assertEqual(tokens_of(text), [(1, 6, 3, "parameter")])

    def test_triple_quotes_on_text_lines_and_empty_body(self):
        self.assertEqual(tokens_of("# %% [markdown]\n\"\"\"\"\"\"\n"), [])
        self.assertEqual(tokens_of("# %% [markdown]\n\"\"\"\n\"\"\"\n"), [])
        self.assertEqual(pairs("# %% [markdown]\n\"\"\"# Title\n\"\"\"\n"), [("# Title", "keyword")])

    def test_unclosed_triple_quote_is_plain_lines(self):
        text = "# %% [markdown]\n\"\"\"\n*a*\n"
        self.assertEqual(pairs(text), [("*a*", "parameter")])

    def test_plain_lines_are_kept_as_they_are(self):
        text = "# %% [markdown]\n*a* plain\n# **b**\n\n  indented *c*\n"
        self.assertEqual(tokens_of(text), [(1, 0, 3, "parameter"), (2, 2, 5, "enumMember"), (4, 11, 3, "parameter")])

    def test_indented_hash_is_not_a_comment_prefix_like_jupytext(self):
        self.assertEqual(tokens_of("# %% [markdown]\n    # *b*\n"), [])
        self.assertEqual(tokens_of("# %% [markdown]\n  # *b*\n"), [(1, 2, 2, "keyword"), (1, 4, 3, "parameter")])

    def test_no_prefix_space_still_removes_the_hash(self):
        text = "# %% [markdown]\n#**b**\n"
        self.assertEqual(tokens_of(text), [(1, 1, 5, "enumMember")])


class MarkdownTokenTests(unittest.TestCase):
    def check(self, source, expected):
        self.assertEqual(pairs(markdown_cell(*source.split("\n"))), expected)

    def test_atx_headings_of_every_level(self):
        for level in range(1, 7):
            with self.subTest(level=level):
                self.check("#" * level + " Title", [("#" * level + " Title", "keyword")])

    def test_seven_hashes_and_hashtags_are_not_headings(self):
        self.check("####### Title", [])
        self.check("#hashtag", [])

    def test_heading_with_closing_hashes_and_inner_tokens(self):
        self.check("## A `b` $c$ ##", [("## A ", "keyword"), ("`b`", "property"), (" ", "keyword"),
                                       ("$", "operator"), ("c", "variable"), ("$", "operator"),
                                       (" ##", "keyword")])

    def test_setext_heading(self):
        self.check("Title\n=====", [("Title", "keyword"), ("=====", "keyword")])

    def test_emphasis_and_strong(self):
        self.check("*a* _b_ **c** __d__", [("*a*", "parameter"), ("_b_", "parameter"),
                                           ("**c**", "enumMember"), ("__d__", "enumMember")])

    def test_nested_emphasis_does_not_overlap(self):
        self.check("**a *b* c**", [("**a ", "enumMember"), ("*b*", "parameter"), (" c**", "enumMember")])

    def test_strikethrough_content_is_scanned_on_its_own(self):
        self.check("~~a *b* $x$~~ `c`", [("*b*", "parameter"), ("$", "operator"), ("x", "variable"),
                                         ("$", "operator"), ("`c`", "property")])
        self.check("~~a $x~~ y$ *b*", [("*b*", "parameter")])

    def test_intraword_underscores_are_not_emphasis(self):
        self.check("snake_case_name and 2*3*4", [("*3*", "parameter")])

    def test_inline_code_spans(self):
        self.check("`a` ``b`c`` ` d `", [("`a`", "property"), ("``b`c``", "property"), ("` d `", "property")])

    def test_emphasis_markers_inside_code_are_literal(self):
        self.check("`*a*` *b*", [("`*a*`", "property"), ("*b*", "parameter")])

    def test_fenced_code_blocks(self):
        self.check("```python\nx = *1*\n```\nafter *a*", [("```python", "property"), ("x = *1*", "property"),
                                                         ("```", "property"), ("*a*", "parameter")])
        self.check("~~~\n$x$\n~~~", [("~~~", "property"), ("$x$", "property"), ("~~~", "property")])

    def test_unclosed_fence_runs_to_the_end_of_the_cell(self):
        self.check("```\nx\n*a*", [("```", "property"), ("x", "property"), ("*a*", "property")])

    def test_indented_code_has_no_tokens(self):
        self.check("para\n\n    *code* $x$\n\nafter", [])

    def test_links_text_and_url(self):
        self.check("[text](http://x.y/a_b \"Title\")", [("text", "function"), ("http://x.y/a_b \"Title\"", "type")])
        self.check("[a *b*](<u v>) ![alt](p.png)", [("a ", "function"), ("*b*", "parameter"), ("<u v>", "type"),
                                                    ("alt", "function"), ("p.png", "type")])

    def test_autolinks_bare_urls_and_references(self):
        self.check("<https://a.b/c_d> https://e.f/g_h", [("https://a.b/c_d", "type"), ("https://e.f/g_h", "type")])
        self.check("[ref]: http://example.com \"t\"", [("ref", "function"), ("http://example.com", "type")])
        self.check("[ref]:\nhttp://example.com\n*a*", [("ref", "function"), ("http://example.com", "type"),
                                                       ("*a*", "parameter")])

    def test_unmatched_brackets_are_plain_text(self):
        self.check("[not a link] and [a](", [])

    def test_list_markers(self):
        self.check("- a\n* b\n+ c\n1. d\n2) e", [("-", "operator"), ("*", "operator"), ("+", "operator"),
                                                ("1.", "operator"), ("2)", "operator")])

    def test_list_item_content_is_tokenized(self):
        self.check("- item *a*\n  more `b`\n  - nested **c**",
                   [("-", "operator"), ("*a*", "parameter"), ("`b`", "property"), ("-", "operator"),
                    ("**c**", "enumMember")])

    def test_block_quote_markers(self):
        self.check("> q *a*\n> > deep\nlazy $x$", [(">", "operator"), ("*a*", "parameter"), (">", "operator"),
                                                  (">", "operator"), ("$", "operator"), ("x", "variable"),
                                                  ("$", "operator")])

    def test_thematic_breaks_and_html_blocks_have_no_tokens(self):
        self.check("---\n***\n___", [])
        self.check("<div>\n*a* $x$\n</div>\n\n*b*", [("*b*", "parameter")])
        self.check("<!-- *a* $x$ -->", [])

    def test_plain_prose_has_no_tokens(self):
        self.check("Just words, numbers 12 and 3.5 - nothing special here.", [])

    def test_multi_line_span_is_split_per_line(self):
        text = markdown_cell("a `code", "more` b")
        self.assertEqual(tokens_of(text), [(1, 4, 5, "property"), (2, 2, 5, "property")])


class MathParityTests(unittest.TestCase):
    parser = mistune.create_markdown(renderer=None, plugins=["math", "table", "strikethrough", "url"])

    @classmethod
    def renderer_math(cls, markdown):
        def walk(tokens):
            for token in tokens:
                if token["type"] in ("inline_math", "block_math"):
                    yield token["raw"], token["type"] == "block_math"
                yield from walk(token.get("children", []))
        return sorted(walk(cls.parser.parse(markdown)[0]))

    def assert_parity(self, markdown):
        self.assertEqual(sorted(highlight.markdown_math(markdown)), self.renderer_math(markdown), repr(markdown))

    CORPUS = [
        "$x$", "$x $", "$ x$", "a $x$ b $y$ c", "$x\ny$", "$a\\$b$", "$a\\$b$ $c$", "$x$5 and $y$", "$x$5", "$1$2$",
        "$$x$$", "$$x\ny$$ after", "$$ x $$ and $$ y $$", "$$x$$ text $$y$$", "$$\nx\n$$", "  $$\nx\n$$",
        "   $$\nx\n$$", "    $$\nx\n$$", "$$\nx\n$$ trailing", "$$\nx\n  $$", "$$\nx\n$$  ", "text\n$$\nx\n$$\nmore",
        "$$\n$$", "$$\nx\n\n$$", "$$ unmatched", "$ unmatched", "$$$$", "$$ $$", "$ $", "$$x\n\ny$$",
        "\\(x\\) and \\[y\\]", "\\begin{align} a \\end{align}", "\\$x\\$", "\\\\$x$",
        "`$x$` and $y$", "$x `a` y$", "`a $x` b$ c`", "``$x$`` $y$", "$x$ `unclosed", "$`x`$", "$``x``$ $y$",
        "```\n$x$\n```\n$y$", "~~~\n$x$\n~~~\n$y$", "```\n$x$", "    $x$\n\n$y$", "para\n    $x$",
        "<div>\n$x$\n</div>\n\n$y$", "<!-- $x$ -->\n$y$", "<!--\n$x$\n-->\n$y$", "<span>$x$</span>",
        "<p>\n$x$\n\n$y$", "text <!-- $x$ --> $y$", "<a title=\"$x$\">$y$</a>", "<http://x/$a$> $b$",
        "[a $x$](http://u/$y$)", "[a]($z$) $w$", "![i $x$](u) $y$", "[a] ($x$)", "[a](b) $x$", "[a\n$x$](u)",
        "http://x.y/$a$ $b$", "# Head $x$", "Setext $x$\n===", "## $x$ ##",
        "> $x$\n> $$\n> y\n> $$", "> $$x$$", "- $x$\n- $$\n  y\n  $$", "- item\n\n  $$\n  y\n  $$\n",
        "1. $x$ and $$y$$", "> - $x$", "* $x$\n\n$$\ny\n$$", ">\t$$ x $$", "> >\t$$ x $$", "> >\t<b>$x$</b>", "> \t$x$", "> >\n\t$$ x $$", "> a\n\t$x$", "> >\n> >\t$$ x $$", "+ $$a\n\tb\n$$", "- $$a\n\t$$b\n  c$$", "~~a $x~~ y$", "~~$x$~~ $y$", "~~a $x$ b~~ $z$", "$a ~~b$ c~~", "~~ a $x$~~", "a~~ $x$ ~~b", "~~a~~~ $x$", "~~~a~~ $x$", "~~a\n$$x~~ y$$", "[~~a](u)~~ $x$", "[ref]:\n$x$", "[ref]:\n$x$\n\n$y$", "[ref]: <u>\n$x$", "para\n[ref]:\n$x$", "[ref]:\n\n$x$", "[ref]: u/$a$ \"t\"\n$x$", "-\ta\n\n    $x$ b", "-\ta\n\n\t$x$ b", "1.\t$x$\n\t$y$",
        "*a $x* y$*", "**$x$**", "_ $x$ _", "$a$$b$", "${}$", "~~$x$~~",
        "| a | b |\n|---|---|\n| $x$ | $y$ |",
        "$x$\n\n$$y$$\n\n$$\nz\n$$", "$$a\\$$b$$", "$$\\\nx$$",
    ]

    def test_corpus_matches_the_renderer(self):
        for markdown in self.CORPUS:
            with self.subTest(markdown=markdown):
                self.assert_parity(markdown)

    def test_rules_from_the_brief_explicitly(self):
        math = highlight.markdown_math
        self.assertEqual(math("$x $"), [("x ", False)])
        self.assertEqual(math("$ x$"), [])
        self.assertEqual(math("$a\\$b$"), [("a\\$b", False)])
        self.assertEqual(math("$x$5 and $y$"), [("5 and ", False)])
        self.assertEqual(math("$$x\ny$$"), [("x\ny", True)])
        self.assertEqual(math("$$\nx\n$$"), [("x", True)])
        self.assertEqual(math("    $$\nx\n$$"), [])
        self.assertEqual(math("\\(x\\) \\[y\\] \\begin{align}a\\end{align}"), [])
        self.assertEqual(math("`$x$` $y$"), [("y", False)])
        self.assertEqual(math("```\n$x$\n```"), [])
        self.assertEqual(math("<!-- $x$ -->"), [])
        self.assertEqual(math("<span>$x$</span>"), [("x", False)])
        self.assertEqual(math("[a $x$](u/$y$)"), [("x", False)])
        self.assertEqual(math("$$ unmatched"), [])

    def test_differential_fuzz_against_the_renderer(self):
        rng = random.Random(20240607)
        pieces = ["$", "$", "$", "$$", "$$", "x", "y", "x ", " x", "a b", " ", "`", "\\", "\\$", "*", "_", "[", "](",
                  ")", "]", "5", "$x$", "$$y$$", "`c`", "[t](u)", "<b>", "</b>", "\n", "\n", "\n$$\n", "<!--", "-->",
                  "|", "&", "^"]
        starts = ["", "", "", "", "- ", "1. ", "  ", "   ", "    ", "# ", "* ", "$$", "$$", "```", "~~~", "<div>",
                  "+ ", "  - "]
        for _ in range(3000):
            lines = [rng.choice(starts) + "".join(rng.choice(pieces) for _ in range(rng.randint(0, 6)))
                     for _ in range(rng.randint(1, 6))]
            markdown = "\n".join(lines)
            with self.subTest(markdown=markdown):
                self.assert_parity(markdown)


class MathTokenTests(unittest.TestCase):
    def check(self, source, expected):
        self.assertEqual(pairs(markdown_cell(*source.split("\n"))), expected)

    def test_inline_math_delimiters_and_content(self):
        self.check("$x^2$", [("$", "operator"), ("x", "variable"), ("^", "operator"), ("2", "number"),
                             ("$", "operator")])

    def test_display_math_on_one_line(self):
        self.check("$$\\frac{a}{b}$$", [("$$", "operator"), ("\\frac", "function"), ("{", "operator"),
                                        ("a", "variable"), ("}{", "operator"), ("b", "variable"),
                                        ("}", "operator"), ("$$", "operator")])

    def test_display_math_block_over_lines(self):
        self.check("$$\na_1\n$$", [("$$", "operator"), ("a", "variable"), ("_", "operator"), ("1", "number"),
                                   ("$$", "operator")])

    def test_inline_display_math_spanning_lines(self):
        text = markdown_cell("x $$a", "b$$ y")
        self.assertEqual(tokens_of(text), [(1, 4, 2, "operator"), (1, 6, 1, "variable"),
                                           (2, 2, 1, "variable"), (2, 3, 2, "operator")])

    def test_commands_symbols_and_numbers(self):
        self.check("$\\alpha\\,\\\\ 3.5 12$", [("$", "operator"), ("\\alpha", "function"), ("\\,", "function"),
                                              ("\\\\", "function"), ("3.5", "number"), ("12", "number"),
                                              ("$", "operator")])

    def test_braces_brackets_and_operators(self):
        self.check("$[a]_{b}^{c}&=-1$", [("$", "operator"), ("[", "operator"), ("a", "variable"),
                                         ("]_{", "operator"), ("b", "variable"), ("}^{", "operator"),
                                         ("c", "variable"), ("}&=-", "operator"), ("1", "number"),
                                         ("$", "operator")])

    def test_environment_names(self):
        self.check("$$\n\\begin{align*}\nx\n\\end{align*}\n$$",
                   [("$$", "operator"), ("\\begin", "function"), ("{", "operator"), ("align*", "type"),
                    ("}", "operator"), ("x", "variable"), ("\\end", "function"), ("{", "operator"),
                    ("align*", "type"), ("}", "operator"), ("$$", "operator")])

    def test_percent_comment_runs_to_the_end_of_the_line_and_escaped_percent_does_not(self):
        self.check("$$\na % note $\nb \\% c\n$$",
                   [("$$", "operator"), ("a", "variable"), ("% note $", "comment"), ("b", "variable"),
                    ("\\%", "function"), ("c", "variable"), ("$$", "operator")])

    def test_backtick_math_and_unicode_letters(self):
        self.check("$`x`$ $αβ$", [("$`", "operator"), ("x", "variable"), ("`$", "operator"),
                                            ("$", "operator"), ("αβ", "variable"), ("$", "operator")])

    def test_math_inside_emphasis_and_link_text(self):
        self.check("*a $x$ b*", [("*a ", "parameter"), ("$", "operator"), ("x", "variable"), ("$", "operator"),
                                 (" b*", "parameter")])
        self.check("[$x$](u)", [("$", "operator"), ("x", "variable"), ("$", "operator"), ("u", "type")])


class TypeTests(unittest.TestCase):
    def test_legend_uses_only_standard_types(self):
        self.assertTrue(set(highlight.TOKEN_TYPES) <= ALLOWED_TYPES)
        self.assertEqual(len(set(highlight.TOKEN_TYPES)), len(highlight.TOKEN_TYPES))
        self.assertTrue(set(highlight.STYLE.values()) <= ALLOWED_TYPES)
        self.assertNotIn("string", highlight.TOKEN_TYPES)

    def test_default_mapping_avoids_string_because_triple_quoted_cells_are_already_strings(self):
        self.assertEqual(highlight.STYLE, {
            "heading": "keyword", "emphasis": "parameter", "strong": "enumMember", "code_span": "property",
            "code_block": "property", "link_text": "function", "link_url": "type", "list_marker": "operator",
            "quote_marker": "operator", "math_delimiter": "operator", "latex_command": "function",
            "latex_operator": "operator", "latex_number": "number", "latex_environment": "type",
            "latex_comment": "comment", "latex_text": "variable",
        })

    def test_every_token_type_index_points_into_the_legend(self):
        text = markdown_cell("# H", "*a* **b** `c` [d](e)", "- f", "> g", "$$", "\\begin{x} a 1 % c", "\\end{x}", "$$")
        used = {kind for *_, kind in tokens_of(text)}
        self.assertEqual(used, {"keyword", "parameter", "enumMember", "property", "function", "operator",
                                "number", "type", "comment", "variable"})


class WireTests(unittest.TestCase):
    SOURCE = "# %% [markdown]\n# # Title *a* $x$\n# - [l](u) `c`\n# > **b**\n# %%\nx = 1\n"

    def test_relative_encoding_matches_the_lsp_spec(self):
        index = highlight.TOKEN_TYPES.index
        data = highlight.semantic_tokens("# %% [markdown]\n# *a* **b**\n#\n# $x$\n")
        self.assertEqual(data, [1, 2, 3, index("parameter"), 0, 0, 4, 5, index("enumMember"), 0,
                                2, 2, 1, index("operator"), 0, 0, 1, 1, index("variable"), 0,
                                0, 1, 1, index("operator"), 0])

    def test_tokens_are_sorted_and_do_not_overlap(self):
        self.assertTrue(assert_wire_valid(self, self.SOURCE))

    def test_crlf_and_lone_cr_give_the_same_tokens(self):
        expected = tokens_of(self.SOURCE)
        self.assertEqual(tokens_of(self.SOURCE.replace("\n", "\r\n")), expected)
        self.assertEqual(tokens_of(self.SOURCE.replace("\n", "\r")), expected)
        endings = ["\r\n", "\n", "\r"]
        parts = self.SOURCE.split("\n")
        mixed = "".join(part + endings[number % 3] for number, part in enumerate(parts[:-1])) + parts[-1]
        self.assertEqual(tokens_of(mixed), expected)

    def test_columns_and_lengths_are_utf16_units(self):
        text = "# %% [markdown]\n# \U0001F600 **b** \U0001F600$x$\n"
        self.assertEqual(tokens_of(text), [(1, 5, 5, "enumMember"), (1, 13, 1, "operator"),
                                           (1, 14, 1, "variable"), (1, 15, 1, "operator")])
        self.assertEqual(pairs(text)[0], ("**b**", "enumMember"))

    def test_astral_characters_inside_tokens_count_two_units(self):
        text = "# %% [markdown]\n# `\U0001F600\U0001F600` *é*\n"
        self.assertEqual(tokens_of(text), [(1, 2, 6, "property"), (1, 9, 3, "parameter")])

    def test_multi_line_tokens_are_split_and_lines_are_counted_after_crlf(self):
        text = "x\r\n# %% [markdown]\r\n# a `b\r\n# c` d\r\n"
        self.assertEqual(tokens_of(text), [(2, 4, 2, "property"), (3, 2, 2, "property")])

    def test_no_token_covers_comment_prefix_marker_or_quotes(self):
        text = ("# %% [markdown] **x**\n# # H\n#\n#**a**\n# %% [md]\n\"\"\"\n# H2 $x$\n\"\"\"\n"
                "# %% [markdown]\n'''**b**'''\n")
        self.assertEqual(tokens_of(text), [
            (1, 2, 3, "keyword"), (3, 1, 5, "enumMember"),
            (6, 0, 5, "keyword"), (6, 5, 1, "operator"), (6, 6, 1, "variable"), (6, 7, 1, "operator"),
            (9, 3, 5, "enumMember"),
        ])

    def test_bom_and_nul_do_not_disturb_positions(self):
        text = "\ufeff# %% [markdown]\n# *a*\x00 **b**\n"
        self.assertEqual(tokens_of(text), [(1, 2, 3, "parameter"), (1, 7, 5, "enumMember")])


class SpeedTests(unittest.TestCase):
    @staticmethod
    def big_file(cells=2500, total=35000):
        per_cell = total // cells
        lines = []
        for number in range(cells):
            lines += ["# %% [markdown]", f"# ## Step {number}", "#", "# The *mean* error `e` and [paper](https://x.y/a).",
                      f"# The loss is $L_{number} = \\sum_i (y_i - \\hat y_i)^2$ per sample.", "# %%",
                      "import numpy as np", "x = np.arange(10)"]
            lines += [f"y{index} = x * {number}" for index in range(per_cell - 8)]
        return "\n".join(lines) + "\n"

    def assert_fast_and_valid(self, text):
        highlight.CELL_CACHE.clear()
        started = time.perf_counter()
        highlight.semantic_tokens(text)
        self.assertLess(time.perf_counter() - started, GUARD_SECONDS)
        assert_wire_valid(self, text)

    def test_text_without_marker_is_a_single_cheap_check(self):
        text = "\n".join(f"value_{index} = {index} # a comment" for index in range(35000))
        with mock.patch.object(highlight, "markdown_bodies", side_effect=AssertionError) as scan:
            self.assertEqual(highlight.semantic_tokens(text), [])
        scan.assert_not_called()

    def test_cache_is_keyed_on_cell_text(self):
        highlight.CELL_CACHE.clear()
        highlight.semantic_tokens(self.big_file(cells=50, total=1000))
        self.assertEqual(highlight.CELL_CACHE.misses, 50)
        self.assertEqual(len(highlight.CELL_CACHE.entries), 50)

    def test_one_cell_edit_reparses_only_that_cell(self):
        highlight.CELL_CACHE.clear()
        text = self.big_file(cells=100, total=2000)
        first = highlight.semantic_tokens(text)
        hits, misses = highlight.CELL_CACHE.hits, highlight.CELL_CACHE.misses
        second = highlight.semantic_tokens(text.replace("## Step 50\n", "## Step 50 edited\n"))
        self.assertEqual(highlight.CELL_CACHE.misses - misses, 1)
        self.assertEqual(highlight.CELL_CACHE.hits - hits, 99)
        self.assertNotEqual(first, second)

    def test_cached_cells_survive_line_shifts(self):
        highlight.CELL_CACHE.clear()
        highlight.semantic_tokens(self.big_file(cells=20, total=400))
        misses = highlight.CELL_CACHE.misses
        highlight.semantic_tokens("import os\n" * 5 + self.big_file(cells=20, total=400))
        self.assertEqual(highlight.CELL_CACHE.misses, misses)

    def test_a_large_file_stays_fully_warm(self):
        highlight.CELL_CACHE.clear()
        text = self.big_file()
        highlight.semantic_tokens(text)
        misses = highlight.CELL_CACHE.misses
        highlight.semantic_tokens(text)
        self.assertEqual(highlight.CELL_CACHE.misses, misses)
        self.assertEqual(highlight.CELL_CACHE.hits, 2500)

    def test_large_file_stays_fast(self):
        text = self.big_file()
        highlight.CELL_CACHE.clear()
        started = time.perf_counter()
        highlight.semantic_tokens(text)
        cold = time.perf_counter() - started
        started = time.perf_counter()
        highlight.semantic_tokens(text.replace("## Step 1250\n", "## Step 1250 edited\n"))
        edit = time.perf_counter() - started
        self.assertLess(cold, GUARD_SECONDS)
        self.assertLess(edit, GUARD_SECONDS)

    def test_pathological_inputs_finish_quickly(self):
        count = 10000
        cases = {
            "200 KB single line": "word " * 40000,
            "200 KB of markup": "*a _b `c $d [e](f) <g> " * 8000,
            "unmatched dollars": "$" * count,
            "dollar space": "$ " * count,
            "dollar backtick": "$` " * count,
            "nested stars": "*" * count,
            "unmatched openers": "*a " * count,
            "open brackets": "[a](x" * count,
            "html comments": "<!--" * count,
            "backticks": "``a`" * count,
            "long title": "[a](u \"" + "t" * 100000,
            "long url": "http://" + "a" * 100000,
            "long math": "$" + "x" * 100000 + "$",
            "long latex": "$$" + "\\frac{a}{b} " * 2000 + "$$",
            "deep quotes": ">" * count,
            "deep lists": "- " * count,
            "long rule": "- " * 50000 + "x",
        }
        for name, line in cases.items():
            with self.subTest(name=name):
                text = "# %% [markdown]\n# " + line + "\n"
                self.assert_fast_and_valid(text)

    def test_pathological_line_structures_finish_quickly(self):
        count = 10000
        cases = {
            "quotes": [">"] * count,
            "list lines": ["- item"] * count,
            "indented openers": ["   $$"] * count,
            "pure openers": ["$$"] * count,
            "open fences": ["```x"] * count,
            "html openers": ["<!--"] * count,
            "reference lines": ["[a]: http://x"] * count,
            "headings": ["# h"] * count,
        }
        for name, lines in cases.items():
            with self.subTest(name=name):
                text = "# %% [markdown]\n" + "".join("# " + line + "\n" for line in lines)
                self.assert_fast_and_valid(text)

    def test_many_markers_and_open_strings_finish_quickly(self):
        count = 10000
        cases = {
            "markers in fence": "# %% [markdown]\n# ```\n" + "# %% x\n" * count + "# ```\n",
            "unclosed triple": "# %% [markdown]\n\"\"\"\n" + "# %% [md]\n" * count,
            "triple lines": "# %%\n" + "\"\"\"\n" * count + "# %% [md]\n# a\n",
            "fenced cells": "".join("# %% [md]\n# ```x\n" for _ in range(count)),
            "plain markers": "# %% [markdown]\n# a\n" * count,
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assert_fast_and_valid(text)


class JupytextTests(unittest.TestCase):
    MARKERS = [
        "# %% [markdown]", "# %% [md]", "# %% Title [markdown]", "#  %%   [markdown]", "    # %% [markdown]",
        "\t# %% [md]", "# %%", "# %% [raw]", "# %%% [md]", "# %% [markdown] tags=[\"x\"]", "# %%[markdown]", "# %%md",
        "# %% [Markdown]", "# %% title", "# %% [md] Title words", "#%% [markdown]", "# %%\t[markdown]",
        "#\t%%\t[md]", "# %% [raw] [md]", "# %% [markdown] [raw]", "# %% [md]  ", "# %%  ", "# %%%% [md]",
        "# % % [md]", "#%%", "# %%\u00a0[md]", "# In[1]:", "# <codecell>",
    ]
    BODIES = [
        "{n} text", "# {n} text", "#{n} text", "#", "", "   ", "# ", "{n} `code`", "# {n} ```", "# {n} ```python",
        "# {n} ~~~", "{n} ```", "{n} ~~~", "# ````", "# ~~~~", "# ```  ", "#    ```", "#  ```", "\"\"\"{n}", "{n} '''",
        "# {n} \"\"\"", "r\"\"\"{n}", "R'''{n}", "# %% {n} inner", "# %% [markdown] {n}", "    # {n} indented",
        "  # {n} two", "x = {n}", "def f{n}():", "{n} don't", "# {n} don't", "x = \"\"\"{n}", "    \"\"\"", "# %%",
        "{n} \"\"\" x \"\"\"", "{n} '''x'''", "# {n} # %% not", "\t# {n}", "# {n} $x$ *a*", "{n} \\\"\"\"",
        "{n} '\"\"\"'", "{n} \"'''\"", "'''", "\"\"\"", "# '''", "# \"\"\"", "{n} # \"\"\"", "# {n} \u00e9\U0001F600",
        "x = '{n}", "'{n}", "\"{n}", "{n} \\", "# {n} \\", "\ufeff# {n}", "{n}\x00 *a*",
    ]
    LINE_NUMBER = re.compile(r"@(\d+)@")

    def generated_lines(self, rng):
        lines = []
        for number in range(rng.randint(1, 16)):
            if rng.random() < 0.3:
                marker = rng.choice(self.MARKERS)
                lines.append(marker if "In[" in marker or "<code" in marker else f"{marker} @{number}@")
            else:
                lines.append(rng.choice(self.BODIES).format(n=f"@{number}@"))
        return lines

    def highlight_lines(self, text, lines):
        found = {}
        for first_line, body in highlight.markdown_bodies(text):
            for body_line, content, _ in highlight.body_entries(body.split("\n"), True):
                tag = self.LINE_NUMBER.search(lines[first_line + body_line]) if first_line + body_line < len(lines) else None
                if tag:
                    found[tag.group(1)] = content
        return found

    def jupytext_lines(self, text):
        found = {}
        for cell in jupytext.reads(text, fmt="py:percent").cells:
            for line in cell.source.split("\n") if cell.cell_type == "markdown" else []:
                tag = self.LINE_NUMBER.search(line)
                if tag:
                    found.setdefault(tag.group(1), line)
        return found

    def test_markdown_cell_lines_and_text_match_jupytext(self):
        rng = random.Random(20240615)
        with_cells = 0
        for _ in range(1500):
            lines = self.generated_lines(rng)
            ending = rng.choice(["\n", "\n", "\r\n"])
            text = ending.join(lines) + ending
            expected = self.jupytext_lines(text)
            with_cells += bool(expected)
            with self.subTest(text=text):
                self.assertEqual(self.highlight_lines(text.replace("\r\n", "\n"), lines), expected)
        self.assertGreater(with_cells, 500)

    def test_a_leading_bom_is_ignored_unlike_jupytext(self):
        text = "\ufeff# %% [markdown]\n# **b**\n"
        self.assertEqual(tokens_of(text), [(1, 2, 5, "enumMember")])
        self.assertEqual([cell.cell_type for cell in jupytext.reads(text, fmt="py:percent").cells], ["code"])


class CacheTests(unittest.TestCase):
    def test_many_distinct_cells_cannot_grow_the_cache_past_its_budget(self):
        cache = highlight.CellCache(200000)
        for number in range(500):
            cache.fetch(f"# Heading {number}\n" + "word *em* $x$ " * 100 + f"{number}")
            self.assertLessEqual(cache.size, cache.budget)
        self.assertGreater(cache.size, cache.budget // 2)
        self.assertLess(len(cache.entries), 500)

    def test_a_cell_larger_than_an_eighth_of_the_budget_is_not_stored(self):
        cache = highlight.CellCache(200000)
        cache.fetch("word " * 10000)
        self.assertEqual(len(cache.entries), 0)
        self.assertEqual(cache.size, 0)

    def test_least_recently_used_cells_are_evicted_first(self):
        cache = highlight.CellCache(18000)
        bodies = [f"*a* {number}\n" + "x" * 1000 for number in range(20)]
        for body in bodies[:10]:
            cache.fetch(body)
        cache.fetch(bodies[0])
        for body in bodies[10:]:
            cache.fetch(body)
        self.assertEqual((cache.hits, cache.misses), (1, 20))
        self.assertIn(bodies[0], cache.entries)
        self.assertNotIn(bodies[1], cache.entries)
        self.assertIn(bodies[-1], cache.entries)

    def test_global_cache_stays_within_budget_for_edits_of_one_large_cell(self):
        highlight.CELL_CACHE.clear()
        for number in range(40):
            highlight.semantic_tokens("# %% [markdown]\n" + "".join(f"# line {number} *em* `c` $x$\n" for _ in range(60)))
            highlight.semantic_tokens("# %% [markdown]\n# " + "$a$ " * 3000 + f"{number}\n")
        self.assertLessEqual(highlight.CELL_CACHE.size, highlight.CELL_CACHE.budget)

    def test_cached_results_do_not_depend_on_cache_state(self):
        text = "# %% [markdown]\n# # H *a*\n# $x$\n# %%\n# %% [md]\n# `c`\n"
        highlight.CELL_CACHE.clear()
        cold = highlight.semantic_tokens(text)
        self.assertEqual(highlight.semantic_tokens(text), cold)
        self.assertEqual(highlight.semantic_tokens("\n\n" + text)[0:2], [cold[0] + 2, cold[1]])


class RobustnessTests(unittest.TestCase):
    ODD_INPUTS = [
        "", "\n", "\r", "\r\n", "# %%", "# %% [markdown]", "# %% [markdown]\n", "# %% [md]\r", "[markdown]", "[md]",
        "﻿", "﻿# %% [markdown]\n# *a*", "\x00", "# %% [markdown]\n\x00\x00\n# \x00*a*\x00",
        "# %% [markdown]\n# \ud800 *a* \udc00", "# %% [markdown]\n# 😀 *a*", "# %% [markdown]\n\t\t# *a*",
        "# %% [markdown]\n# ```", "# %% [markdown]\n# ~~~~\n# x", "# %% [markdown]\n\"\"\"", "# %% [markdown]\n'''*a*",
        "# %% [markdown]\n\"\"\"*a*\"\"\"\"\"\"", "# %% [markdown]\n# $", "# %% [markdown]\n# $$", "# %% [markdown]\n# $$\n#",
        "# %% [markdown]\n# $$\n# x\n# $$$$", "# %% [markdown]\n# `", "# %% [markdown]\n# [", "# %% [markdown]\n# ](",
        "# %% [markdown]\n# <", "# %% [markdown]\n# <!--", "# %% [markdown]\n# \\", "# %% [markdown]\n# \\\\\\",
        "# %% [markdown]\n# > > > > > > > > > > > > > > >", "# %% [markdown]\n# - - - - - - - - - - - -",
        "# %% [markdown]\n# 1. 2. 3.", "# %% [markdown]\n# #", "# %% [markdown]\n# =", "# %% [markdown]\n# ---",
        "# %% [markdown]\r\n# a\r# b\n# c\r\n", "# %% [markdown]\n# $\\begin{", "# %% [markdown]\n# $$\\begin{a}$$",
        "# %% [markdown]\n# $%$", "# %% [markdown]\n# $\\$", "# %% [md]\n\"\"\"\n\"\"\"\n\"\"\"", "x" * 1000 + "[md]",
        "# %% [markdown] " + "{" * 100 + "\n# a", "# %% [markdown] =\n# a", "# %%" + " " * 100000 + "[md]",
    ]

    def test_odd_inputs_never_raise_and_stay_valid(self):
        for text in self.ODD_INPUTS:
            with self.subTest(text=text[:60]):
                assert_wire_valid(self, text)

    def test_every_prefix_of_a_rich_document_is_valid(self):
        document = ("# %% [markdown]\n# # Title *a* **b** `c`\n# - [l](u) $x$ $$\\frac{a}{b}$$\n# > q\n# ```\n# %% x\n# ```\n"
                    "# %% [md]\n\"\"\"\n$$\n\\begin{align}a\\end{align}\n$$\n\"\"\"\n# %%\nx = 1\n")
        for end in range(len(document) + 1):
            assert_wire_valid(self, document[:end])

    def test_random_inputs_from_risky_pieces(self):
        rng = random.Random(20240611)
        pieces = [
            "# %% [markdown]\n", "# %% [md]\n", "# %%\n", "# %% [raw]\n", "# %%\n", "\"\"\"", "'''", "r\"\"\"", "```", "~~~",
            "# ", "#", "\n", "\n", "\r\n", "\r", "\t", "﻿", "\x00", "\ud800", "\U0001F600", "é", "$", "$$", "$$\n",
            "\\", "\\$", "\\\\", "`", "``", "*", "**", "_", "[", "](", ")", "]", "!", "<", ">", "<!--", "-->", "<div>",
            "http://u.v/", "x", "a b", " ", "  ", "    ", "1.", "- ", "> ", "# ", "|", "---", "===", "%", "{", "}", "^",
            "&", "\\begin{a}", "\\end{a}", "\\frac", "5", "[markdown]", "[md]", " %% ",
        ]
        for _ in range(4000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
            with self.subTest(text=text[:80]):
                assert_wire_valid(self, text)

    def test_no_token_on_a_non_cell_line_in_structured_random_inputs(self):
        rng = random.Random(20240612)
        pieces = ["x", "a b", " ", "*", "**", "_", "`", "$", "$$", "\\", "[", "](", ")", "]", "<", ">", "&", "^", "5",
                  "\u00e9", "\U0001F600", "{", "}", "%", "!", "|", "http://u.v/", "<!--", "-->", "1.", "- ", "> ", "# "]
        markers = [("# %% [markdown]", True), ("# %% [md] title", True), ("# %%", False), ("# %% [raw]", False),
                   ("# %% [Markdown]", False), ("# %% some title", False)]
        for _ in range(1500):
            lines, allowed, prefixes = [], set(), {}
            for _ in range(rng.randint(1, 6)):
                marker, is_markdown = rng.choice(markers)
                lines.append(marker)
                body = ["".join(rng.choice(pieces) for _ in range(rng.randint(0, 6))) for _ in range(rng.randint(0, 5))]
                body = [line for line in body if not SPEC_MARKER.match("# " + line) and not SPEC_MARKER.match(line)
                        and not any(bad in line for bad in ("```", "~~~"))]
                quoted = is_markdown and rng.random() < 0.3
                if quoted:
                    lines.append('"""')
                for line in body:
                    if is_markdown:
                        allowed.add(len(lines))
                        prefixes[len(lines)] = 0 if quoted else 2 if line.startswith("# ") else line.startswith("#")
                    lines.append(line)
                if quoted:
                    lines.append('"""')
            text = rng.choice(["\n", "\r\n"]).join(lines)
            for line, start, length, kind in assert_wire_valid(self, text):
                self.assertIn(line, allowed, text)
                self.assertGreaterEqual(start, prefixes[line], text)


if __name__ == "__main__":
    unittest.main()
