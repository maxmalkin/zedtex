import re
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from sys import getsizeof
from threading import Lock
from unicodedata import category

from mistune.helpers import BLOCK_TAGS, HTML_ATTRIBUTES, HTML_TAGNAME, PRE_TAGS
from mistune.plugins.math import BLOCK_MATH_PATTERN, INLINE_MATH_PATTERN


STYLE = {
    "heading": "keyword",
    "emphasis": "parameter",
    "strong": "enumMember",
    "code_span": "property",
    "code_block": "property",
    "link_text": "function",
    "link_url": "type",
    "list_marker": "operator",
    "quote_marker": "operator",
    "math_delimiter": "operator",
    "latex_command": "function",
    "latex_operator": "operator",
    "latex_number": "number",
    "latex_environment": "type",
    "latex_comment": "comment",
    "latex_text": "variable",
}

TOKEN_TYPES = list(dict.fromkeys(STYLE.values()))
TYPE_INDEX = {name: TOKEN_TYPES.index(style) for name, style in STYLE.items()}

MAX_DEPTH = 8
EMPHASIS_LOOKBACK = 50
CACHE_BUDGET = 16 * 1024 * 1024
CACHE_ENTRY_OVERHEAD = 256
WHITESPACE = " \r\n\t\f"
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"

MARKER = re.compile(
    r"^[^\S\n]*#[^\S\n]*(?:%%(%*)[^\S\n](.*)|(?:%%|<codecell>|In\[[0-9 ]*\]:?)[^\S\n]*$)", re.M)
FENCE_CLOSER = re.compile(r"^(?:# ?)? {0,3}(`{3,}|~{3,})[^\S\n]*$", re.M)
CODE_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
CODE_DANGER = re.compile(r"\"\"\"|'''")
TEXT_DANGER = re.compile(r"\"\"\"|'''|```|~~~")
QUOTE_OR_HASH = re.compile(r"[\"'#]")
ASTRAL = re.compile("[\U00010000-\U0010ffff]")

START = re.compile(
    r"(?P<indent> {4}.| *\t.)"
    r"|(?P<fence> {0,3}(?:`{3,}|~{3,}))"
    r"|(?P<heading> {0,3}\#{1,6}(?!\#)(?=[ \t]|$))"
    r"|(?P<setext> {0,3}[=-]+[ \t]*$)"
    r"|(?P<rule> {0,3}(?:(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})$)"
    r"|(?P<quote> {0,3}>)"
    r"|(?P<math> {0,3}\$\$)"
    r"|(?P<item> {0,3}(?:[*+-]|\d{1,9}[.)])(?=[ \t]|$))"
    r"|(?P<reference> {0,3}\[)"
    r"|(?P<html> {0,3}<)"
    r"|(?P<blank>[ \t\v\f]*$)"
)
BLANK = re.compile(r"[ \t\v\f]*$")
INDENT = re.compile(r" {4}.| *\t.")
FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})[ \t]*(.*?)$")
RULE = re.compile(r" {0,3}(?:(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})$")
ITEM = re.compile(r"( {0,3})([*+-]|\d{1,9}[.)])(?=[ \t]|$)")
QUOTE = re.compile(r"( {0,3})>( ?)")
QUOTE_NEEDS_MARKER = re.compile(r"[ \t\v\f]*$|(?: {4}| *\t).|(?: {0,3})(?:`{3,}|~{3,})")
BLOCK_MATH = re.compile(BLOCK_MATH_PATTERN, re.M)
MATH_OPENER = re.compile(r" {0,3}\$\$[ \t]*")
MATH_CLOSER = re.compile(r"\$\$[ \t]*")
REFERENCE = re.compile(
    r" {0,3}\[((?:[^\\\[\]]|\\.){1,999})\]:[ \t]*(<[^<>]*>|[^\s<]\S*)"
    r"(?:[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?[ \t]*$")
HTML_START = re.compile(r" {0,3}(</?" + HTML_TAGNAME + r"|<!--|<\?|<![A-Z]|<!\[CDATA\[)")
BLOCK_HTML = re.compile(
    r" {0,3}(?:</?(?:" + "|".join(BLOCK_TAGS + PRE_TAGS) + r")(?:[ \t]+|$)|<!--|<\?|<![A-Z]|<!\[CDATA\[)")
OPEN_TAG_END = re.compile(HTML_ATTRIBUTES + r"[ \t]*>[ \t]*$")
CLOSE_TAG_END = re.compile(r"[ \t]*>[ \t]*$")
HTML_END_MARKERS = {"<!--": "-->", "<?": "?>", "<![CDATA[": "]]>"}

SPECIAL = re.compile(r"[\\$`*_\[\]<]|!\[|https?://")
ESCAPE = re.compile(r"(?:\\[!-/:-@\[-`{-~])+")
BACKTICKS = re.compile(r"`+")
CLOSING_BRACKETS = re.compile(r"\]+")
EMPHASIS_RUN = re.compile(r"\*+|_+")
AUTO_LINK = re.compile(r"<[A-Za-z][A-Za-z0-9.+-]{1,31}:[^<>\x00-\x20]*>")
BARE_URL = re.compile(r"""https?://[^\s<]+[^<.,:;"')\]\s]""")
INLINE_TAG = re.compile(r"<" + HTML_TAGNAME + HTML_ATTRIBUTES + r"\s*/?>|</" + HTML_TAGNAME + r"\s*>")
DECLARATION = re.compile(r"<![A-Z]+[ \t\n\r\f]")
SIMPLE_DESTINATION = re.compile(r"[^\s()\\<\x00-\x1f\x7f]*")
LINK_TITLE_OPENERS = {'"': '"', "'": "'", "(": ")"}
ASCII_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

INLINE_MATH = re.compile(INLINE_MATH_PATTERN)
_ALTERNATIVES = re.split(r"\|(?=\\\$)", INLINE_MATH_PATTERN)
INLINE_MATH_WITHOUT_CODE = (
    re.compile(_ALTERNATIVES[0] + "|" + _ALTERNATIVES[2]) if len(_ALTERNATIVES) == 3 else INLINE_MATH)

LATEX = re.compile(
    r"(?P<comment>%[^\n]*)"
    r"|(?P<environment>(?P<keyword>\\(?:begin|end))(?P<gap>\s*)\{(?P<name>[^{}\n]*)\})"
    r"|(?P<command>\\(?:[A-Za-z]+\*?|[^\sA-Za-z]))"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<word>[^\W\d_]+)"
    r"|(?P<operator>(?:[^\s\w\\%]|_)+|\\)"
)
LATEX_STYLES = {
    "comment": "latex_comment",
    "command": "latex_command",
    "number": "latex_number",
    "word": "latex_text",
    "operator": "latex_operator",
}


def cell_kind(title):
    if title is None or "[" not in title:
        return "code"
    cuts = [index for index in (title.find("{"), title.find("=")) if index >= 0]
    head = title[:min(cuts)] if cuts else title
    if "[markdown]" in head:
        return "markdown"
    if "[raw]" in head:
        return "raw"
    return "markdown" if "[md]" in head else "code"


def read_quotes(line, triple):
    if triple is None and line.lstrip().startswith("#"):
        return triple
    single = None
    triple_start = -1
    for match in QUOTE_OR_HASH.finditer(line):
        index = match.start()
        if line[index] == "#":
            if single is None and triple is None:
                break
            continue
        single, triple, triple_start = quote_step(line, index, single, triple, triple_start)
    return triple


def quote_step(line, index, single, triple, triple_start):
    char = line[index]
    if line[index - 1:index] == "\\" or (single is not None and single != char):
        return single, triple, triple_start
    if single == char:
        return None, triple, triple_start
    if line[index - 2:index + 1] == char * 3 and index >= triple_start + 3:
        if triple == char:
            return single, None, index
        return (single, char, index) if triple is None else (single, triple, triple_start)
    return (single, triple, triple_start) if triple is not None else (char, triple, triple_start)


def uncomment(line):
    if line.startswith("# "):
        return line[2:]
    return line[1:] if line.startswith("#") else line


class Fences:
    def __init__(self, text):
        self.text = text
        self.last_seen = None

    def closed_after(self, offset, char, size):
        if self.last_seen is None:
            self.last_seen = {}
            for match in FENCE_CLOSER.finditer(self.text):
                run = match.group(1)
                self.last_seen[(run[0], len(run))] = match.start()
        return any(seen >= offset and run_char == char and length >= size
                   for (run_char, length), seen in self.last_seen.items())


def fence_after(line, fence, closed_later):
    match = CODE_FENCE.match(uncomment(line))
    if fence:
        closes = (match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]
                  and not match.group(2).strip())
        return None if closes else fence
    if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
        opening = (match.group(1)[0], len(match.group(1)))
        if closed_later(*opening):
            return opening
    return None


def advance(text, begin, end, is_text, state, fences):
    triple, fence = state
    danger = TEXT_DANGER if is_text else CODE_DANGER
    if triple is None and fence is None and not danger.search(text, begin, end):
        return state
    offset = begin
    for line in text[begin:end].split("\n")[:-1]:
        offset += len(line) + 1
        was_quoted = triple is not None
        if '"' in line or "'" in line:
            triple = read_quotes(line, triple)
        if is_text and not was_quoted and ("`" in line or "~" in line):
            fence = fence_after(line, fence, lambda char, size: fences.closed_after(offset, char, size))
    return triple, fence


def find_cells(text):
    fences = Fences(text)
    cells = []
    state = (None, None)
    is_text = False
    scanned = 0
    for marker in MARKER.finditer(text):
        start = marker.start()
        state = advance(text, scanned, start, is_text, state, fences)
        scanned = start
        if state == (None, None):
            kind = cell_kind(marker.group(2))
            cells.append((marker, kind))
            is_text = kind != "code"
    return cells


def markdown_bodies(text):
    cells = find_cells(text)
    bodies = []
    counted_offset = counted_lines = 0
    for index, (marker, kind) in enumerate(cells):
        if kind != "markdown":
            continue
        body_start = marker.end() + 1
        body_end = cells[index + 1][0].start() - 1 if index + 1 < len(cells) else len(text)
        counted_lines += text.count("\n", counted_offset, marker.end())
        counted_offset = marker.end()
        if body_end > body_start:
            bodies.append((counted_lines + 1, text[body_start:body_end]))
    return bodies


def triple_quoted_entries(lines):
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first == len(lines):
        return None
    last = len(lines) - 1
    while not lines[last].strip():
        last -= 1
    head = lines[first].lstrip()
    column = len(lines[first]) - len(head)
    tail = lines[last].rstrip()
    for prefix in ("", "r", "R"):
        for quote in ('"""', "'''"):
            left = prefix + quote
            fits = first != last or len(tail) - column >= len(left) + len(quote)
            if head.startswith(left) and tail.endswith(quote) and fits:
                return quoted_lines(lines, first, last, column + len(left), len(quote))
    return None


def quoted_lines(lines, first, last, start, quote_size):
    tail = lines[last].rstrip()
    if first == last:
        return [(first, tail[start:len(tail) - quote_size], start)]
    entries = [(first, lines[first][start:], start)] if lines[first][start:] else []
    entries.extend((index, lines[index], 0) for index in range(first + 1, last))
    final = tail[:-quote_size]
    if final:
        entries.append((last, final, 0))
    return entries


def body_entries(lines, quotable):
    quoted = triple_quoted_entries(lines) if quotable else None
    if quoted is not None:
        return quoted
    return [(index, line[2:], 2) if line.startswith("# ")
            else (index, line[1:], 1) if line.startswith("#")
            else (index, line, 0) for index, line in enumerate(lines)]


def flatten(spans):
    spans.sort(key=lambda span: (span[0], -span[1]))
    previous_end = 0
    for start, end, _ in spans:
        if start < previous_end:
            break
        previous_end = end
    else:
        return spans
    flat = []
    stack = []
    cursor = 0
    for start, end, kind in spans:
        if end <= start:
            continue
        cursor = close_spans(stack, start, flat, cursor)
        if stack and start > cursor:
            flat.append((cursor, start, stack[-1][1]))
        cursor = max(cursor, start)
        stack.append((min(end, stack[-1][0]) if stack else end, kind))
    close_spans(stack, float("inf"), flat, cursor)
    return flat


def close_spans(stack, limit, flat, cursor):
    while stack and stack[-1][0] <= limit:
        end, kind = stack.pop()
        if end > cursor:
            flat.append((cursor, end, kind))
            cursor = end
    return cursor


def contiguous(pieces):
    if len(pieces) == 1:
        return True
    position = pieces[0][1]
    for text, base in pieces:
        if base != position:
            return False
        position = base + len(text) + 1
    return True


def indent_width(text):
    width = 0
    for char in text:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def columns_to_characters(text, columns):
    width = 0
    for index, char in enumerate(text):
        if width >= columns:
            return index
        width += 1 if char == " " else 4 - width % 4
    return len(text)


def closes_fence(text, char, size):
    stripped = text.lstrip(" ")
    if len(text) - len(stripped) > 3:
        return False
    run = stripped.rstrip(" \t")
    return len(run) >= size and not run.strip(char)


def valid_fence(text):
    match = FENCE.match(text)
    return bool(match) and not (match.group(2)[0] == "`" and "`" in match.group(3))


def interrupts_container(text):
    match = START.match(text)
    kind = match.lastgroup if match else None
    if kind in ("heading", "quote"):
        return True
    if kind == "fence":
        return valid_fence(text)
    return kind == "html" and BLOCK_HTML.match(text) is not None


def list_break(text, bound, bullet):
    if len(text) - len(text.lstrip(" ")) > bound:
        return None
    if RULE.match(text):
        return "end"
    item = ITEM.match(text)
    if item:
        return "next" if item.group(2)[-1] == bullet else "end"
    return "end" if interrupts_container(text) else None


def interrupts_quote(text):
    if BLANK.match(text) or RULE.match(text) or ITEM.match(text) or valid_fence(text):
        return True
    return BLOCK_HTML.match(text) is not None


def skip_indented(lines, index):
    index += 1
    while index < len(lines):
        text = lines[index][0]
        if not INDENT.match(text) and text.strip():
            break
        index += 1
    return index


def trim_closing_hashes(content):
    end = len(content)
    while end and content[end - 1] == "#":
        end -= 1
    if end == len(content) or (end and not content[end - 1].isspace()):
        return content
    return content[:end].rstrip()


def tokenize_latex(text, start, end, emit):
    for match in LATEX.finditer(text, start, end):
        kind = match.lastgroup
        if kind != "environment":
            emit(match.start(), match.end(), LATEX_STYLES[kind])
            continue
        emit(match.start("keyword"), match.end("keyword"), "latex_command")
        emit(match.end("gap"), match.end("gap") + 1, "latex_operator")
        emit(match.start("name"), match.end("name"), "latex_environment")
        emit(match.end("name"), match.end("name") + 1, "latex_operator")


def is_punctuation(char):
    return char in ASCII_PUNCTUATION or (char > "\x7f" and category(char)[0] in "PS")


def delimiter_roles(char, before, after):
    before_space, after_space = before.isspace(), after.isspace()
    before_punct, after_punct = is_punctuation(before), is_punctuation(after)
    left = not after_space and (not after_punct or before_space or before_punct)
    right = not before_space and (not before_punct or after_space or after_punct)
    if char == "*":
        return left, right
    return left and (not right or before_punct), right and (not left or after_punct)


def opener_matches(opener, closer):
    if opener[2] != closer[2]:
        return False
    if (opener[4] or closer[3]) and (opener[5] + closer[5]) % 3 == 0:
        return opener[5] % 3 == 0 and closer[5] % 3 == 0
    return True


class Inline:
    def __init__(self, text, emit, math):
        self.text = text
        self.emit = emit
        self.math = math
        self.brackets = []
        self.last_link = None
        self.delimiters = []
        self.code_runs = None
        self.missing = set()
        self.budget = 8 * len(text) + 4096
        self.last_code_closer = None

    def run(self):
        text = self.text
        position = 0
        while True:
            match = SPECIAL.search(text, position)
            if match is None:
                break
            position = INLINE_HANDLERS[text[match.start()]](self, match)
        self.resolve_emphasis(self.delimiters)

    def escape(self, match):
        found = ESCAPE.match(self.text, match.start())
        return found.end() if found else match.start() + 1

    def dollar(self, match):
        text, start = self.text, match.start()
        if text.startswith("`", start + 1):
            if self.last_code_closer is None:
                self.last_code_closer = text.rfind("`$")
            pattern = INLINE_MATH if self.last_code_closer >= start + 2 else INLINE_MATH_WITHOUT_CODE
        else:
            pattern = INLINE_MATH_WITHOUT_CODE
        found = pattern.match(text, start)
        if found is None:
            return start + 1
        self.record_math(found)
        return found.end()

    def record_math(self, found):
        group = found.lastgroup
        raw_start, raw_end = found.start(group), found.end(group)
        self.math.append((found.group(group), group == "display_math_text"))
        self.emit(found.start(), raw_start, "math_delimiter")
        self.emit(raw_end, found.end(), "math_delimiter")
        tokenize_latex(self.text, raw_start, raw_end, self.emit)

    def backtick(self, match):
        start = match.start()
        run_end = BACKTICKS.match(self.text, start).end()
        size = run_end - start
        closer = self.text.find("`" * size, run_end)
        if closer >= 0 and self.text[closer - 1] != "`" and self.text[closer + size:closer + size + 1] != "`":
            self.emit(start, closer + size, "code_span")
            return closer + size
        if self.code_runs is None:
            self.code_runs = {}
            for run in BACKTICKS.finditer(self.text):
                self.code_runs.setdefault(run.end() - run.start(), []).append(run.start())
        positions = self.code_runs.get(size, [])
        index = bisect_left(positions, run_end)
        if index == len(positions):
            return run_end
        self.emit(start, positions[index] + size, "code_span")
        return positions[index] + size

    def delimiter(self, match):
        text, start = self.text, match.start()
        end = EMPHASIS_RUN.match(text, start).end()
        before = text[start - 1] if start else " "
        after = text[end] if end < len(text) else " "
        can_open, can_close = delimiter_roles(text[start], before, after)
        self.delimiters.append([start, end, text[start], can_open, can_close, end - start])
        return end

    def open_bracket(self, match):
        image = match.group() == "!["
        self.brackets.append([match.end(), image, True, self.last_link, len(self.delimiters)])
        if not image:
            self.last_link = self.brackets[-1]
        return match.end()

    def discard_brackets(self, count):
        stack = self.brackets
        if count >= len(stack):
            stack.clear()
            self.last_link = None
            return
        removed = stack[-count:]
        del stack[-count:]
        for bracket in removed:
            if not bracket[1]:
                self.last_link = bracket[3]
                break

    def close_bracket(self, match):
        start = match.start()
        run_end = CLOSING_BRACKETS.match(self.text, start).end()
        followed = self.text.startswith("(", run_end)
        literal = run_end - start - (1 if followed else 0)
        if literal:
            self.discard_brackets(literal)
        if not followed or not self.brackets:
            return run_end
        opener = self.brackets.pop()
        if not opener[1]:
            self.last_link = opener[3]
        target = self.link_target(run_end) if opener[2] else None
        if target is None:
            return run_end
        self.finish_link(opener, run_end - 1, target)
        return target[2]

    def finish_link(self, opener, close, target):
        self.emit(opener[0], close, "link_text")
        self.emit(target[0], target[1], "link_url")
        self.resolve_emphasis(self.delimiters[opener[4]:])
        del self.delimiters[opener[4]:]
        if not opener[1]:
            bracket = self.last_link
            while bracket is not None and bracket[2]:
                bracket[2] = False
                bracket = bracket[3]

    def link_target(self, open_paren):
        limit = min(len(self.text), open_paren + self.budget)
        found = parse_link_target(self.text, open_paren + 1, limit)
        self.budget = max(0, self.budget - ((found[2] if found else limit) - open_paren))
        return found

    def angle(self, match):
        text, start = self.text, match.start()
        auto = AUTO_LINK.match(text, start)
        if auto:
            self.emit(start + 1, auto.end() - 1, "link_url")
            return auto.end()
        end = self.html_end(start)
        return start + 1 if end is None else end

    def html_end(self, start):
        text = self.text
        if text.startswith("<!-->", start):
            return start + 5
        if text.startswith("<!--->", start):
            return start + 6
        for opener, closer in (("<!--", "-->"), ("<?", "?>"), ("<![CDATA[", "]]>")):
            if text.startswith(opener, start):
                return self.after_closer(closer, start + len(opener))
        if text.startswith("<!", start):
            declaration = DECLARATION.match(text, start)
            return self.after_closer(">", declaration.end()) if declaration else None
        tag = INLINE_TAG.match(text, start)
        return tag.end() if tag else None

    def after_closer(self, closer, position):
        if closer in self.missing:
            return None
        found = self.text.find(closer, position)
        if found < 0:
            self.missing.add(closer)
            return None
        return found + len(closer)

    def bare_url(self, match):
        text, start = self.text, match.start()
        found = BARE_URL.match(text, start)
        if found is None:
            return start + 1
        end = found.end()
        if self.brackets:
            close = text.find("]", start, end)
            end = close if close >= 0 else end
        self.emit(start, end, "link_url")
        return end

    def resolve_emphasis(self, runs):
        stack = []
        for closer in runs:
            if closer[4]:
                self.close_with(stack, closer)
            if closer[3] and closer[1] > closer[0]:
                stack.append(closer)

    def close_with(self, stack, closer):
        while closer[1] > closer[0]:
            index = self.find_opener(stack, closer)
            if index is None:
                return
            opener = stack[index]
            use = 2 if opener[1] - opener[0] >= 2 and closer[1] - closer[0] >= 2 else 1
            self.emit(opener[1] - use, closer[0] + use, "strong" if use == 2 else "emphasis")
            opener[1] -= use
            closer[0] += use
            del stack[index + 1:]
            if opener[1] == opener[0]:
                stack.pop()

    def find_opener(self, stack, closer):
        for index in range(len(stack) - 1, max(len(stack) - 1 - EMPHASIS_LOOKBACK, -1), -1):
            if opener_matches(stack[index], closer):
                return index
        return None


INLINE_HANDLERS = {
    "\\": Inline.escape, "$": Inline.dollar, "`": Inline.backtick, "*": Inline.delimiter,
    "_": Inline.delimiter, "[": Inline.open_bracket, "!": Inline.open_bracket,
    "]": Inline.close_bracket, "<": Inline.angle, "h": Inline.bare_url,
}


def skip_characters(text, position, limit, characters):
    while position < limit and text[position] in characters:
        position += 1
    return position


def parse_link_target(text, start, limit):
    position = skip_characters(text, start, limit, " \t")
    if position < limit and text[position] == "\n":
        position = skip_characters(text, position + 1, limit, " \t")
    first = position
    position = destination_end(text, position, limit)
    if position is None:
        return None
    last = position
    title_end = title_end_after(text, position, limit)
    position = skip_characters(text, title_end or position, limit, " \t\n\r\f")
    if position >= limit or text[position] != ")":
        return None
    return first, title_end or last, position + 1


def escaped_pair(text, position, limit):
    return text[position] == "\\" and position + 1 < limit and text[position + 1] in ASCII_PUNCTUATION


def angle_destination_end(text, position, limit):
    position += 1
    while position < limit:
        char = text[position]
        if char == ">":
            return position + 1
        if char in "<\n\r\x00":
            return None
        position += 2 if escaped_pair(text, position, limit) else 1
    return None


def destination_end(text, position, limit):
    if position >= limit:
        return None
    if text[position] == "<":
        return angle_destination_end(text, position, limit)
    simple = SIMPLE_DESTINATION.match(text, position, limit)
    if simple.end() < limit and text[simple.end()] in ") \t\n\r\f":
        return simple.end()
    depth = 0
    while position < limit:
        char = text[position]
        if char in " \t\n\r\f" or (char == ")" and depth == 0):
            break
        if ord(char) < 32 or char == "\x7f" or (char == "(" and depth == 20):
            return None
        depth += (char == "(") - (char == ")")
        position += 2 if escaped_pair(text, position, limit) else 1
    return position if depth == 0 and position < limit else None


def blank_line_follows(text, position, limit):
    while position < limit and text[position] in " \t":
        position += 1
    return position < limit and text[position] == "\n"


def title_end_after(text, position, limit):
    if position >= limit or text[position] not in " \t\n\r\f":
        return None
    position = skip_characters(text, position, limit, " \t\n\r\f")
    if position >= limit or text[position] not in LINK_TITLE_OPENERS:
        return None
    opener = text[position]
    position += 1
    while position < limit:
        char = text[position]
        if char == "\x00" or (opener == "(" and char == "(") or (char == "\n" and blank_line_follows(text, position + 1, limit)):
            return None
        if char == LINK_TITLE_OPENERS[opener]:
            return position + 1
        position += 2 if escaped_pair(text, position, limit) else 1
    return None


class Scanner:
    def __init__(self):
        self.spans = []
        self.math = []
        self.closer_lines = None

    def add(self, start, end, key):
        if end > start:
            self.spans.append((start, end, TYPE_INDEX[key]))

    def line_emitter(self, base):
        spans = self.spans

        def emit(start, end, key):
            if end > start:
                spans.append((base + start, base + end, TYPE_INDEX[key]))
        return emit

    def region_emitter(self, pieces, lead):
        if contiguous(pieces):
            return self.line_emitter(pieces[0][1] + lead)
        starts = []
        position = 0
        for text, _ in pieces:
            starts.append(position)
            position += len(text) + 1

        def emit(start, end, key):
            start, end = start + lead, end + lead
            index = bisect_right(starts, start) - 1
            while start < end and index < len(pieces):
                text, base = pieces[index]
                stop = min(end, starts[index] + len(text))
                self.add(base + start - starts[index], base + stop - starts[index], key)
                index += 1
                if index < len(pieces):
                    start = max(start, starts[index])
        return emit

    def inline_text(self, pieces, style=None):
        joined = "\n".join([text for text, _ in pieces])
        text = joined.strip(WHITESPACE)
        if not text:
            return
        emit = self.region_emitter(pieces, len(joined) - len(joined.lstrip(WHITESPACE)))
        if style:
            emit(0, len(text), style)
        if SPECIAL.search(text):
            Inline(text, emit, self.math).run()

    def flush(self, paragraph, style=None):
        if paragraph:
            self.inline_text(paragraph, style)
            paragraph.clear()

    def blocks(self, lines, depth=0):
        paragraph = []
        index = 0
        while index < len(lines):
            text, _ = lines[index]
            match = START.match(text)
            handler = BLOCK_HANDLERS.get(match.lastgroup) if match else None
            following = handler(self, lines, index, paragraph, depth) if handler else None
            if following is None:
                paragraph.append(lines[index])
                index += 1
            else:
                index = following
        self.flush(paragraph)

    def blank(self, lines, index, paragraph, depth):
        self.flush(paragraph)
        return index + 1

    def indented(self, lines, index, paragraph, depth):
        return None if paragraph else skip_indented(lines, index)

    def rule(self, lines, index, paragraph, depth):
        self.flush(paragraph)
        return index + 1

    def code_line(self, text, base):
        stripped = text.strip()
        if stripped:
            start = base + len(text) - len(text.lstrip())
            self.add(start, start + len(stripped), "code_block")

    def fenced(self, lines, index, paragraph, depth):
        if not valid_fence(lines[index][0]):
            return None
        self.flush(paragraph)
        marker = FENCE.match(lines[index][0]).group(2)
        end = index + 1
        while end < len(lines) and not closes_fence(lines[end][0], marker[0], len(marker)):
            end += 1
        for text, base in lines[index:end + 1]:
            self.code_line(text, base)
        return end + 1

    def heading(self, lines, index, paragraph, depth):
        text, base = lines[index]
        self.flush(paragraph)
        hashes = START.match(text).end()
        body = text[hashes:]
        content = trim_closing_hashes(body.strip(ASCII_WHITESPACE))
        lead = len(body) - len(body.lstrip(ASCII_WHITESPACE))
        indent = len(text) - len(text.lstrip(" "))
        self.add(base + indent, base + len(text.rstrip(ASCII_WHITESPACE)), "heading")
        self.inline_text([(content, base + hashes + lead)])
        return index + 1

    def setext(self, lines, index, paragraph, depth):
        text, base = lines[index]
        if paragraph:
            self.flush(paragraph, "heading")
            self.add(base + len(text) - len(text.lstrip()), base + len(text.rstrip()), "heading")
            return index + 1
        if RULE.match(text):
            return index + 1
        return self.items(lines, index, paragraph, depth) if ITEM.match(text) else None

    def quote(self, lines, index, paragraph, depth):
        if depth >= MAX_DEPTH:
            return None
        self.flush(paragraph)
        body, end = self.quote_body(lines, index)
        self.blocks(body, depth + 1)
        return end

    def quote_body(self, lines, index):
        body = []
        end = index
        needs_marker = previous_blank = False
        while end < len(lines):
            text, base = lines[end]
            match = QUOTE.match(text)
            if match:
                self.add(base + match.end(1), base + match.end(1) + 1, "quote_marker")
                content = text[match.end():]
                body.append((content, base + match.end()))
                needs_marker = needs_marker if end > index else bool(QUOTE_NEEDS_MARKER.match(content))
                previous_blank = not content.strip()
            elif needs_marker or previous_blank or interrupts_quote(text):
                break
            else:
                body.append((text, base))
            end += 1
        return body, end

    def display_math(self, lines, index, paragraph, depth):
        if depth:
            return None
        text, base = lines[index]
        single = BLOCK_MATH.match(text)
        if single and single.group("math_text_single") is not None:
            return self.single_line_math(single, base, paragraph, index)
        if MATH_OPENER.fullmatch(text) is None:
            return None
        end = self.math_closer(lines, index)
        return None if end is None else self.multi_line_math(lines, index, end, paragraph)

    def math_closer(self, lines, index):
        if self.closer_lines is None:
            self.closer_lines = [position for position, (line, _) in enumerate(lines)
                                 if MATH_CLOSER.fullmatch(line)]
        found = bisect_left(self.closer_lines, index + 2)
        return self.closer_lines[found] if found < len(self.closer_lines) else None

    def multi_line_math(self, lines, index, end, paragraph):
        self.flush(paragraph)
        text, base = lines[index]
        body = lines[index + 1:end]
        raw = "\n".join([line for line, _ in body])
        opener = len(text) - len(text.lstrip(" "))
        self.add(base + opener, base + opener + 2, "math_delimiter")
        self.add(lines[end][1], lines[end][1] + 2, "math_delimiter")
        self.math.append((raw, True))
        tokenize_latex(raw, 0, len(raw), self.region_emitter(body, 0))
        return end + 1

    def single_line_math(self, found, base, paragraph, index):
        self.flush(paragraph)
        start, end = found.start("math_text_single"), found.end("math_text_single")
        self.math.append((found.group("math_text_single"), True))
        self.add(base + start - 2, base + start, "math_delimiter")
        self.add(base + end, base + end + 2, "math_delimiter")
        tokenize_latex(found.string, start, end, self.line_emitter(base))
        return index + 1

    def reference(self, lines, index, paragraph, depth):
        text, base = lines[index]
        if paragraph or "]:" not in text:
            return None
        found = REFERENCE.match(text)
        if found is None:
            return None
        self.add(base + found.start(1), base + found.end(1), "link_text")
        self.add(base + found.start(2), base + found.end(2), "link_url")
        return index + 1

    def html(self, lines, index, paragraph, depth):
        found = HTML_START.match(lines[index][0])
        if found is None:
            return None
        end = html_block_end(lines, index, found, bool(paragraph))
        if end is not None:
            self.flush(paragraph)
        return end

    def items(self, lines, index, paragraph, depth):
        if depth >= MAX_DEPTH:
            return None
        text, _ = lines[index]
        match = ITEM.match(text)
        marker = match.group(2)
        empty = not text[match.end():].strip()
        if paragraph and (empty or (marker[-1] in ".)" and int(marker[:-1]) != 1)):
            return None
        self.flush(paragraph)
        position = index
        while True:
            following, end = self.item(lines, position, marker[-1], depth)
            if following is None:
                return end
            position = following

    def item(self, lines, index, bullet, depth):
        text, base = lines[index]
        match = ITEM.match(text)
        width = match.end()
        self.add(base + len(match.group(1)), base + width, "list_marker")
        content, continue_width = item_first_line(text, base, width)
        following, end = collect_item(lines, index + 1, content, (continue_width, width, bullet))
        while content and not content[-1][0].strip():
            content.pop()
        self.blocks(content, depth + 1)
        return following, end


def item_first_line(text, base, width):
    rest = text[width:]
    if not rest.strip():
        return [], width + 1
    gap = indent_width(rest)
    gap = 1 if gap >= 5 else gap
    strip = min(gap, len(rest) - len(rest.lstrip(" \t")))
    return [(rest[strip:], base + width + strip)], width + gap


def collect_item(lines, position, content, shape):
    continue_width, width, bullet = shape
    started_blank = not content
    previous_blank = False
    while position < len(lines):
        line, line_base = lines[position]
        if BLANK.match(line):
            content.append(("", line_base))
            previous_blank = True
        elif indent_width(line) >= continue_width:
            if previous_blank and started_blank and not any(part.strip() for part, _ in content):
                break
            strip = columns_to_characters(line, continue_width)
            content.append((line[strip:], line_base + strip))
            previous_blank = False
        else:
            outcome = list_break(line, min(width, 3), bullet)
            if outcome or previous_blank:
                return (position if outcome == "next" else None), position
            content.append((line, line_base))
        position += 1
    return None, position


def html_block_end(lines, index, found, paragraph_open):
    marker = found.group(1)
    end_marker = HTML_END_MARKERS.get(marker) or (">" if marker.startswith("<!") else None)
    if end_marker:
        return after_marker(lines, index, end_marker, found.end())
    closing = marker.startswith("</")
    name = marker[2 if closing else 1:].lower()
    if not closing and name in PRE_TAGS:
        return after_marker(lines, index, "</" + name + ">", found.end())
    if name in BLOCK_TAGS:
        return until_blank(lines, index)
    tail = CLOSE_TAG_END if closing else OPEN_TAG_END
    if paragraph_open or tail.match(lines[index][0], found.end()) is None:
        return None
    return until_blank(lines, index)


def after_marker(lines, index, marker, column):
    end = index
    position = lines[index][0].find(marker, column)
    while position < 0 and end + 1 < len(lines):
        end += 1
        position = lines[end][0].find(marker)
    return end + 1


def until_blank(lines, index):
    end = index + 1
    while end < len(lines) and BLANK.match(lines[end][0]) is None:
        end += 1
    return end


BLOCK_HANDLERS = {
    "blank": Scanner.blank, "indent": Scanner.indented, "fence": Scanner.fenced,
    "heading": Scanner.heading, "setext": Scanner.setext, "rule": Scanner.rule,
    "quote": Scanner.quote, "math": Scanner.display_math, "item": Scanner.items,
    "reference": Scanner.reference, "html": Scanner.html,
}


def line_starts(texts):
    starts = []
    position = 0
    for text in texts:
        starts.append(position)
        position += len(text) + 1
    return starts


def convert_spans(flat, entries, starts):
    texts = [text for _, text, _ in entries]
    astral = {} if ASTRAL.search("".join(texts)) else None
    tokens = []
    line = 0
    line_end = starts[0] + len(texts[0])
    for start, end, kind in flat:
        while line + 1 < len(starts) and starts[line + 1] <= start:
            line += 1
            line_end = starts[line] + len(texts[line])
        if end <= line_end and astral is None:
            body_line, _, offset = entries[line]
            tokens.append((body_line, offset + start - starts[line], end - start, kind))
        else:
            tokens.extend(split_span(start, end, kind, line, entries, starts, astral))
    return tokens


def split_span(start, end, kind, row, entries, starts, astral):
    pieces = []
    while start < end and row < len(entries):
        stop = min(end, starts[row] + len(entries[row][1]))
        if stop > start:
            pieces.append(line_token(entries[row], starts[row], start, stop, kind, astral, row))
        row += 1
        if row < len(entries):
            start = max(start, starts[row])
    return pieces


def line_token(entry, line_start, start, stop, kind, astral, row):
    body_line, text, offset = entry
    first, last = start - line_start, stop - line_start
    if astral is None:
        return (body_line, offset + first, last - first, kind)
    if row not in astral:
        astral[row] = [match.start() for match in ASTRAL.finditer(text)]
    before = bisect_left(astral[row], first)
    inside = bisect_left(astral[row], last) - before
    return (body_line, offset + first + before, last - first + inside, kind)


def scan_entries(entries):
    texts = [text for _, text, _ in entries]
    starts = line_starts(texts)
    scanner = Scanner()
    scanner.blocks(list(zip(texts, starts)))
    return scanner, starts


def encode_wire(tokens):
    wire = array("i")
    line = start = 0
    for token_line, token_start, length, kind in tokens:
        delta = token_line - line
        wire.extend((delta, token_start if delta else token_start - start, length, kind, 0))
        line, start = token_line, token_start
    return wire, line, start


EMPTY_CELL = (array("i"), 0, 0)


def compute_cell(body):
    try:
        quotable = '"""' in body or "'''" in body
        entries = body_entries(body.split("\n"), quotable)
        if not entries:
            return EMPTY_CELL
        scanner, starts = scan_entries(entries)
        return encode_wire(convert_spans(flatten(scanner.spans), entries, starts))
    except Exception:
        return EMPTY_CELL


class CellCache:
    def __init__(self, budget):
        self.budget = budget
        self.lock = Lock()
        self.clear()

    def clear(self):
        self.entries = OrderedDict()
        self.size = self.hits = self.misses = 0

    def fetch(self, body):
        with self.lock:
            entry = self.entries.get(body)
            if entry is not None:
                self.entries.move_to_end(body)
                self.hits += 1
                return entry[0]
            self.misses += 1
        result = compute_cell(body)
        self.store(body, result)
        return result

    def store(self, body, result):
        cost = getsizeof(body) + getsizeof(result[0]) + CACHE_ENTRY_OVERHEAD
        if cost > self.budget // 8:
            return
        with self.lock:
            if body in self.entries:
                return
            self.entries[body] = (result, cost)
            self.size += cost
            while self.size > self.budget:
                self.size -= self.entries.popitem(last=False)[1][1]


CELL_CACHE = CellCache(CACHE_BUDGET)


def markdown_math(markdown):
    texts = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    scanner = Scanner()
    scanner.blocks(list(zip(texts, line_starts(texts))))
    return scanner.math


def semantic_tokens(text):
    if "[markdown]" not in text and "[md]" not in text:
        return []
    try:
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text.startswith("\ufeff"):
            text = text[1:]
        data = []
        previous_line = previous_start = 0
        for first_line, body in markdown_bodies(text):
            wire, last_line, last_start = CELL_CACHE.fetch(body)
            if not wire:
                continue
            begin = len(data)
            data.extend(wire)
            delta_line = first_line + wire[0] - previous_line
            data[begin] = delta_line
            data[begin + 1] = wire[1] if delta_line else wire[1] - previous_start
            previous_line, previous_start = first_line + last_line, last_start
        return data
    except Exception:
        return []
