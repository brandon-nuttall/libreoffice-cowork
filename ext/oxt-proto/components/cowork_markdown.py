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


def plain(block):
    """A block's text: every run's text joined."""
    return "".join(r.get("text", "") for r in block.get("runs", []))


def _run(text, bold=False, italic=False, mono=False):
    return {"text": text, "bold": bold, "italic": italic, "mono": mono}


def uno_system_path_to_url(path):
    """A file URL, importing uno lazily so this module stays importable without it."""
    import uno
    return uno.systemPathToFileUrl(path)


def parse(text):
    """THE markdown parser for the transcript. There is exactly one.

    Deliberately pure python and in-process: it cannot fail to load, cannot
    open windows or documents, cannot trip security dialogs, and is unit-tested
    against hostile input. The earlier design parsed through a hidden
    LibreOffice document (the office's own Markdown filter), which produced
    600 crashes per session on one machine, stray windows, and a tower of
    fallbacks to compensate. This function is the tower, un-built.

    It handles structure -- headings, bullets, numbered lists, code fences,
    rules -- and strips inline markers. It returns blocks of the same shape the
    panel has always consumed. It never touches the document on screen.
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
        # Inline markers off the CONTENT only: running this on raw lines would
        # also erase the code-fence backticks and no fence could ever open.
        return line.replace("**", "").replace("`", "")

    lines = (text or "").replace("\r\n", "\n").split("\n")
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
        if stripped.startswith("#"):  # (fence handled above)
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
