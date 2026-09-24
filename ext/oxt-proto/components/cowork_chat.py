"""Pure layout core for the bubble chat transcript.

Companion to `docs/CHAT_UI_DESIGN.md` (Phase 1). This module owns the ARITHMETIC:
stacking, gaps, visibility within the viewport, scrollbar range and autoscroll
targets. It imports no UNO, touches no controls, and is covered by
`tests/test-chat-layout.py`. Heights are not predicted here: the panel observes
them from the toolkit's own wrap (C2/C3 in the design doc) and hands them in.

Units throughout are the container's 1/100 mm.
"""

# Block kinds, matching cowork_markdown's output vocabulary.
PARAGRAPH = "paragraph"
HEADING = "heading"
BULLET = "bullet"
CODE = "code"
RULE = "rule"
# Pseudo-kinds for the empty state, rendered through the same pipeline.
CENTER = "center"
CENTER_MUTED = "center_muted"

# -- geometry constants (panel units) --------------------------------------- #

MARGIN = 6           # outer margin; the transcript container sits at this
USER_INSET = 14      # left inset of the user bubble, suggesting the side
BLOCK_PAD = 2        # vertical padding added below observed content height
BUBBLE_PAD = 6       # extra below-content padding inside a user bubble
GAP_BLOCK = 8        # between paragraphs of the same speaker
GAP_HEADING_BEFORE = 24
GAP_HEADING_AFTER = 4
GAP_MSG = 60         # between speakers — the strongest visual break
GAP_CODE = 6
RULE_HEIGHT = 4
BAR_W = 10           # vertical scrollbar width
BAR_GAP = 2
EDGE = 4             # right-side breathing room inside the container
# Wheel: one notch moves this many calibrated lines. Three feels like a chat
# client; one feels sticky.
WHEEL_LINES = 3

# Colors -- one explicit palette, painted by the panel itself.
# BackgroundColor=-1 ("toolkit default") made contrast a property of whichever
# theme the user runs; every surface here is a stated colour, so the chat reads
# identically in light and dark themes.
PANEL_BG = 0x1B1C1F        # the transcript surface, clearly darker than any theme
USER_BG = 0x543F33         # the user's bubble: the accent hue, unmistakable
USER_TEXT = 0xFFFFFF
CODE_BG = 0x232529
CODE_TEXT = 0xDDDDDD
TEXT = 0xE6E7E9            # assistant body text
MUTED = 0x909090
RULE_BG = 0x3A3D44
PLAIN_BG = PANEL_BG
TEXT_PLAIN = TEXT

# Dash binding: agents write -- and - separators heavily; LibreOffice breaks
# lines AFTER a dash, leaving a dangling dash at end-of-line. Binding the dash
# to the following word with a no-break space pushes the break to BEFORE the
# dash, where a dash leading the next line reads fine.
NBSP = "\u00a0"
_BINDINGS = (("\u2014 ", "\u2014" + NBSP), ("\u2013 ", "\u2013" + NBSP),
             (" - ", " -" + NBSP), ("- ", "-" + NBSP))


def bind_dashes(text):
    """Stop a dash from dangling at end-of-line."""
    if not text:
        return text
    for before, after in _BINDINGS:
        text = text.replace(before, after)
    return text


def style_for(block):
    """Position, colour and font decisions for one block — pure.

    Returns dict with x, width_in (inset from both sides of the container),
    bg, fg, mono, size_delta, weight, align, and whether content height is
    observed (rules are fixed-height).
    """
    kind = block.get("kind", PARAGRAPH)
    who = block.get("who")
    base = {"mono": False, "size_delta": 0, "weight": 100.0, "align": 0,
            "fg": TEXT, "bg": PANEL_BG, "observed": True}
    if kind == HEADING:
        base.update(weight=150.0, size_delta=3, gap_before=GAP_HEADING_BEFORE,
                    gap_after=GAP_HEADING_AFTER)
        x, wi = 0, EDGE
    elif kind == BULLET:
        base.update(prefix="\u2022 ")
        x, wi = 8, EDGE + 4
    elif kind == CODE:
        base.update(mono=True, bg=CODE_BG, fg=CODE_TEXT,
                    gap_before=GAP_CODE, gap_after=GAP_CODE)
        x, wi = 8, EDGE + 4
    elif kind == RULE:
        base.update(bg=RULE_BG, observed=False, fixed_h=RULE_HEIGHT)
        x, wi = 8, EDGE + 8
    elif kind in (CENTER, CENTER_MUTED):
        base.update(align=1)
        if kind == CENTER_MUTED:
            base["fg"] = MUTED
        x, wi = 0, EDGE
    elif who == "you":
        base.update(bg=USER_BG, fg=USER_TEXT, gap_before=GAP_MSG,
                    gap_after=GAP_MSG, pad_b=BUBBLE_PAD)
        x, wi = USER_INSET, EDGE + 4
    else:
        base.update(gap_before=GAP_BLOCK, gap_after=GAP_BLOCK)
        x, wi = 0, EDGE
    base.setdefault("prefix", "")
    base.setdefault("gap_before", GAP_BLOCK)
    base.setdefault("gap_after", GAP_BLOCK)
    base.setdefault("pad_b", BLOCK_PAD)
    base["kind"] = kind
    base["who"] = who
    base["x"] = x
    base["width_in"] = wi
    return base


def gap_between(prev, cur):
    """Vertical gap to insert before block `cur`, given the previous block."""
    if prev is None:
        return 0
    if cur.get("who") != prev.get("who"):
        return GAP_MSG
    cs = style_for(cur)
    return cs.get("gap_before", GAP_BLOCK)


def plan(rows, view, offset=None):
    """Stack measured rows and cut them to the viewport.

    `rows` is a list of dicts: {key, h (final padded height), gap (before this
    row)}, already in order. Returns {rows: positioned + visible flags, total,
    used_offset, max_offset}. `offset=None` means autoscroll-to-newest.
    """
    total = sum(r["h"] + r.get("gap", 0) for r in rows)
    max_offset = max(total - view, 0)
    used = max_offset if offset is None else max(0, min(offset, max_offset))

    y = 0
    out = []
    for row in rows:
        y += row.get("gap", 0)
        top = y
        bottom = y + row["h"]
        out.append({
            "key": row["key"],
            "x": row.get("x", 0),
            "width_in": row.get("width_in", EDGE),
            "y": top,
            "h": row["h"],
            # Visible when any part overlaps [offset, offset+view].
            "visible": bottom > used and top < used + view,
        })
        y = bottom
    return {"rows": out, "total": total, "used_offset": used,
            "max_offset": max_offset}


def copy_text(blocks, to_plain_text):
    """Clipboard text: delegate to the existing clean-paragraph renderer."""
    return to_plain_text(blocks, 200)
