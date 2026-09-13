"""Markdown rendering for the sidebar transcript.

A single edit control cannot render markdown: it holds plain text, and its font
properties apply to the whole control rather than to a run. The other obvious
routes are closed too — `UnoControlRichTextControl` and `UnoControlRichText` are
not creatable from the panel's context (they return None, the same trap as
`com.sun.star.awt.Timer`), so there is nothing that takes RTF or HTML.

What DOES work is per-control formatting: `UnoControlFixedTextModel` accepts
FontWeight, FontHeight, FontName, FontSlant and TextColor, and the transcript
already lives in a scrollable UnoControlContainer. So the conversation is rendered
as a vertical stack of small styled labels — a heading is a bold larger label, a
code line is monospace, a bullet is indented — which is what a chat view actually
is.

This module is deliberately free of UNO imports: it turns transcript entries into
a flat list of styled blocks, and the panel turns those into controls. That split
is what makes the format layer testable without a GUI.
"""

import re

# Block kinds the renderer understands.
HEADING = "heading"
PARAGRAPH = "paragraph"
BULLET = "bullet"
CODE = "code"
RULE = "rule"
SPEAKER = "speaker"

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*```")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")

# Inline emphasis is stripped rather than styled: a control's font applies to the
# whole control, so `**bold**` inside a sentence cannot be made bold without
# splitting the sentence across controls and re-implementing line breaking. Bold
# is therefore reserved for blocks that are bold in their entirety — headings and
# speaker labels — which is where it carries the most meaning.
_INLINE = (
    (re.compile(r"\*\*\*(.+?)\*\*\*"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),   # keep link text, drop URL
)


def strip_inline(text):
    """Remove markdown emphasis markers, keeping the words."""
    for pattern, replacement in _INLINE:
        text = pattern.sub(replacement, text)
    return text.strip()


def parse(text, speaker=None):
    """Turn one message into a list of styled blocks.

    Each block is `{"kind": str, "text": str}`; the panel decides how to draw it.
    A leading label such as "Cowork:" is emitted as its own `speaker` block so the
    transcript reads as a conversation rather than a wall of prose.
    """
    blocks = []
    if speaker:
        blocks.append({"kind": SPEAKER, "text": speaker})

    lines = text.split("\n")
    paragraph = []
    in_code = False
    code_lines = []

    def flush_paragraph():
        if paragraph:
            joined = " ".join(part.strip() for part in paragraph if part.strip())
            if joined:
                blocks.append({"kind": PARAGRAPH, "text": strip_inline(joined)})
            del paragraph[:]

    for raw in lines:
        line = raw.rstrip()

        if _FENCE_RE.match(line):
            if in_code:
                blocks.append({"kind": CODE, "text": "\n".join(code_lines)})
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                in_code = True
            continue

        if in_code:
            code_lines.append(raw)
            continue

        if not line.strip():
            flush_paragraph()
            continue

        if _HR_RE.match(line):
            flush_paragraph()
            blocks.append({"kind": RULE, "text": ""})
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            blocks.append({"kind": HEADING, "text": strip_inline(heading.group(2)),
                           "level": len(heading.group(1))})
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            blocks.append({"kind": BULLET, "text": strip_inline(bullet.group(1))})
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            flush_paragraph()
            blocks.append({"kind": BULLET,
                           "text": "%s. %s" % (ordered.group(1),
                                               strip_inline(ordered.group(2)))})
            continue

        paragraph.append(line)

    if in_code and code_lines:
        blocks.append({"kind": CODE, "text": "\n".join(code_lines)})
    flush_paragraph()
    return blocks


def layout(entries):
    """Flatten transcript entries into the blocks the panel should draw.

    `entries` are the panel's conversation records: `{"kind", "text"}` where kind
    is `you`, `cowork`, `status` or anything else (drawn verbatim).
    """
    blocks = []
    for entry in entries:
        kind = entry.get("kind")
        text = entry.get("text") or ""
        if kind == "you":
            blocks.extend(parse(text, speaker="You"))
        elif kind == "cowork":
            blocks.extend(parse(text, speaker="Cowork"))
        elif kind == "status":
            blocks.append({"kind": SPEAKER, "text": text})
        else:
            blocks.extend(parse(text))
    return blocks


# Vertical gaps, in the container's units (1/100 mm). The panel scales these.
GAP_TIGHT = 20
GAP_NORMAL = 90
GAP_BLOCK = 160


def gap_before(block, previous):
    """Blank space above a block, so the message grouping is visible."""
    if previous is None:
        return 0
    if block["kind"] == SPEAKER:
        return GAP_BLOCK
    if block["kind"] in (HEADING, RULE, CODE):
        return GAP_NORMAL
    if previous["kind"] in (SPEAKER, HEADING):
        return GAP_TIGHT
    return GAP_TIGHT
