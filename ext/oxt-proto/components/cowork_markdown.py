"""Markdown rendering for the sidebar transcript.

MARKDOWN IS PARSED BY LIBREOFFICE ITSELF.

The first implementation was a regex parser in this file. It worked, and it was
still the wrong answer: LibreOffice 26.2 ships a Markdown import filter
(``generic_Markdown``, extensions ``md markdown``, media type ``text/markdown``),
so the office already contains a maintained parser with the full syntax. It
reports what it understood through the ordinary document model:

    "# A Heading"     -> paragraph style "Heading 1", CharWeight 150
    "**bold**"        -> a text run with CharWeight 150.0
    "*italic*"        -> a text run with CharPosture ITALIC
    "- item"          -> body paragraph carrying NumberingRules
    "```code```"      -> paragraph style "Preformatted Text"

So this module does NOT parse markdown. It hands the text to that filter through a
hidden Writer document and reads the result back as styled blocks. Our syntax
support is then exactly as good as the office's, and it disappears if we get it
wrong.

The cost: parsing needs a live office, so it cannot be unit-tested as a pure
function. The block model and the layout maths are covered by tests; the filtering
itself is exercised in the running application.
"""

import os
import tempfile

# Block kinds the renderer understands.
HEADING = "heading"
PARAGRAPH = "paragraph"
BULLET = "bullet"
CODE = "code"
RULE = "rule"
SPEAKER = "speaker"

# Paragraph styles the Markdown filter produces, mapped to our kinds.
_HEADING_STYLES = {"Heading 1": 1, "Heading 2": 2, "Heading 3": 3,
                   "Heading 4": 4, "Heading 5": 5, "Heading 6": 6}
_CODE_STYLES = {"Preformatted Text", "Preformatted"}

# CharWeight at or above this is bold; FontWeight.BOLD is 150.0.
_BOLD_WEIGHT = 140.0


def _run(text, bold=False, italic=False, mono=False):
    return {"text": text, "bold": bold, "italic": italic, "mono": mono}


def uno_system_path_to_url(path):
    """A file URL, importing uno lazily so this module stays importable without it."""
    import uno
    return uno.systemPathToFileUrl(path)


_PARSE_FAILURES = 0       # consecutive office-parse failures, module-wide
_PARSE_DISABLE_AFTER = 3


def parse_with_office(ctx, text):
    global _PARSE_FAILURES
    # Circuit breaker: on this reporter's Wayland session the hidden-document
    # load raised forever, 600 times per conversation, each attempt costing a
    # window flash and making the sidebar felt broken. After three consecutive
    # failures parsing via the office is disabled for the session and the plain
    # fallback takes over.
    if _PARSE_FAILURES >= _PARSE_DISABLE_AFTER:
        raise RuntimeError("office parsing disabled after repeated failures")

    """Parse markdown with LibreOffice's own filter and return styled blocks.

    `ctx` is a UNO component context, which the panel has. Returns a list of
    ``{"kind", "runs", "level", "ordered"}``; ``runs`` carry per-run emphasis so
    the panel can draw them with different fonts.
    """
    from com.sun.star.beans import PropertyValue

    desktop = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", ctx)

    handle, path = tempfile.mkstemp(suffix=".md", prefix="cowork-")
    os.close(handle)
    try:
        # The filter takes a URL, so the markdown has to touch disk once. That
        # temp file is the entire cost of using the office's parser.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

        def prop(name, value):
            item = PropertyValue()
            item.Name = name
            item.Value = value
            return item

        from com.sun.star.document import MacroExecutionMode
        try:
            doc = desktop.loadComponentFromURL(
                uno_system_path_to_url(path), "_blank", 0,
            (prop("Hidden", True), prop("FilterName", "Markdown"),
             prop("ReadOnly", True),
             # Parsing must never run code. A temp chat document has no macro,
             # and the user should never see a macro-security dialog because of
             # the sidebar.
             prop("MacroExecutionMode", MacroExecutionMode.NEVER_EXECUTE)))
            load_ok = True
        except Exception:
            _PARSE_FAILURES += 1
            raise
        try:
            result = _read_blocks(doc)
            _PARSE_FAILURES = 0
            return result
        finally:
            try:
                doc.close(False)
            except Exception:
                pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_blocks(doc):
    blocks = []
    enumeration = doc.getText().createEnumeration()
    while enumeration.hasMoreElements():
        paragraph = enumeration.nextElement()
        try:
            style = paragraph.ParaStyleName
        except Exception:
            style = ""
        runs = _read_runs(paragraph, style)
        if not "".join(r["text"] for r in runs).strip():
            continue

        kind = PARAGRAPH
        level = None
        ordered = False
        if style in _HEADING_STYLES:
            kind = HEADING
            level = _HEADING_STYLES[style]
        elif style in _CODE_STYLES:
            kind = CODE
        elif _is_list_item(paragraph):
            kind = BULLET
            ordered = _is_ordered(paragraph)

        blocks.append({"kind": kind, "runs": runs, "level": level,
                       "ordered": ordered})
    return blocks


def _is_list_item(paragraph):
    try:
        return paragraph.NumberingRules is not None
    except Exception:
        return False


def _is_ordered(paragraph):
    """Ordered lists carry a numbering type; bullets do not.

    Only cosmetic here (a "1." prefix versus a dot), so failing to read it
    degrades to a bullet rather than losing the item.
    """
    try:
        rules = paragraph.NumberingRules
        if rules is None:
            return False
        rule = rules.getByIndex(0)
        getter = getattr(rule, "getPropertyValue", None)
        return bool(getter("NumberingType")) if getter is not None else False
    except Exception:
        return False


def _read_runs(paragraph, style):
    """Flatten a paragraph into styled runs, preserving emphasis."""
    mono = style in _CODE_STYLES
    try:
        enumeration = paragraph.createEnumeration()
    except Exception:
        return [_run(paragraph.getString(), mono=mono)]

    runs = []
    while enumeration.hasMoreElements():
        portion = enumeration.nextElement()
        try:
            text = portion.getString()
        except Exception:
            continue
        if not text:
            continue
        bold = False
        italic = False
        try:
            bold = float(portion.CharWeight) >= _BOLD_WEIGHT
        except Exception:
            pass
        try:
            italic = "ITALIC" in str(portion.CharPosture).upper()
        except Exception:
            pass
        runs.append(_run(text, bold=bold, italic=italic, mono=mono))
    return runs or [_run(paragraph.getString(), mono=mono)]


def layout(entries, parse):
    """Flatten transcript entries into blocks, using the supplied parser.

    `parse(markdown)` is passed in so this function needs no live office.
    """
    blocks = []
    for entry in entries:
        kind = entry.get("kind")
        text = entry.get("text") or ""
        if kind in ("you", "cowork"):
            blocks.append({"kind": SPEAKER,
                           "runs": [_run("You" if kind == "you" else "Cowork")]})
            blocks.extend(parse(text))
        elif kind == "status":
            blocks.append({"kind": SPEAKER, "runs": [_run(text)]})
        else:
            blocks.extend(parse(text))
    return blocks


def plain(block):
    """The block's text with no styling — for logs and tests."""
    return "".join(run["text"] for run in block.get("runs", []))


# Vertical gaps, in the container's units (1/100 mm).
GAP_TIGHT = 20
GAP_NORMAL = 90
GAP_BLOCK = 160


def gap_before(block, previous):
    """Blank space above a block, so message grouping stays visible."""
    if previous is None:
        return 0
    if block["kind"] == SPEAKER:
        return GAP_BLOCK
    if block["kind"] in (HEADING, RULE, CODE):
        return GAP_NORMAL
    if previous["kind"] in (SPEAKER, HEADING):
        return GAP_TIGHT
    return GAP_TIGHT


def parse_fallback(text):
    """Parse markdown WITHOUT the office — pure python, structural only.

    The office parse is best (real filter, real styling) but it cannot be used
    everywhere: on one Wayland session it failed permanently, and the previous
    "fallback" to a single plain paragraph then displayed RAW markdown --
    literal ** markers and ## headings -- in the transcript. This parser keeps
    structure when the office route is unavailable: headings, bullets,
    numbered lists, code fences and rules. Inline emphasis is dropped, exactly
    as the sidebar design intends.
    """
    blocks = []

    def para(line):
        blocks.append({"kind": "paragraph",
                       "runs": [{"text": line, "bold": False, "italic": False,
                                 "mono": False}]})

    # Inline markers are stripped: the office parser removes them, and the
    # fallback must not show literal ** or backticks just because the office
    # route is unavailable. Runs stay single-text (no emphasised spans).
    def clean(line):
        return line.replace("**", "").replace("`", "")

    lines = clean((text or "")).replace("\r\n", "\n").split("\n")
    in_code = False
    code_lines = []
    for line in lines:
        stripped = line.strip()
        if in_code:
            if stripped.startswith("```"):
                blocks.append({"kind": "code",
                               "runs": [{"text": "\n".join(code_lines),
                                         "bold": False, "italic": False,
                                         "mono": True}]})
                code_lines = []
                in_code = False
            else:
                code_lines.append(line)
            continue
        if stripped.startswith("```"):
            in_code = True
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            words = stripped[level:].strip()
            blocks.append({"kind": "heading",
                           "runs": [{"text": words, "bold": True,
                                     "italic": False, "mono": False}],
                           "level": level})
            continue
        if stripped in ("---", "***", "___") and len(set(stripped)) == 1:
            blocks.append({"kind": "rule", "runs": []})
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            # A run of dashes in prose ("--- ") is text, not a bullet; the
            # check above already routed exact rules.
            blocks.append({"kind": "bullet",
                           "runs": [{"text": stripped[2:].strip(), "bold": False,
                                     "italic": False, "mono": False}]})
            continue
        ordered = None
        for marker in ("1. ", "1) "):
            pass
        if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)" \
                and stripped[2] == " ":
            blocks.append({"kind": "bullet",
                           "runs": [{"text": stripped[3:].strip(), "bold": False,
                                     "italic": False, "mono": False}]})
            continue
        para(line.rstrip())
    if code_lines:
        blocks.append({"kind": "code",
                       "runs": [{"text": "\n".join(code_lines), "bold": False,
                                 "italic": False, "mono": True}]})
    return blocks
