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
# Bubble geometry as FRACTIONS of the pane width, mirroring WhatsApp: each
# side's messages hug that side with a modest opposite gap.
USER_X_FRAC = 0.20        # the user's bubble starts 20% from the left
USER_RIGHT_FRAC = 0.05    # and ends 5% from the right
MODEL_X_FRAC = 0.05       # the model's content starts 5% from the left
MODEL_RIGHT_FRAC = 0.20   # and ends 20% from the right
BLOCK_PAD = 2        # vertical padding added below observed content height
BUBBLE_PAD = 6       # extra below-content padding inside a user bubble
GAP_BLOCK = 4        # between blocks inside ONE model turn (near-solid:
                     # the seam filler paints these, so the turn reads as a
                     # single box)
GAP_HEADING_BEFORE = 14
GAP_HEADING_AFTER = 2
GAP_MSG = 60         # between speakers — the strongest visual break
GAP_CODE = 4
# Inner padding: the label is inset inside its bubble by PAD_H on each side,
# and colour strips fill the ring between bubble edge and text.
PAD_H = 14           # left/right: pure strip
PAD_V = 8            # top strip adds to the label's intrinsic ~6 -> ~14
PAD_V_BOTTOM = 12    # bottom strip adds to the label's intrinsic ~2-3 -> ~14
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
MODEL_BG = 0x2B2E34        # the model's bubble: plainly lighter than the pane
CODE_BG = 0x1F2126
CODE_TEXT = 0xDDDDDD
TEXT = 0xE6E7E9            # assistant body text
MUTED = 0x909090
RULE_BG = 0x3A3D44
COMPOSER_BG = 0x27262B      # the pill the composer sits in (matches MODEL_BG's family)
PLAIN_BG = MODEL_BG = 0x2B2E34
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

    Returns dict with x_frac/right_frac (mirrored bubble geometry as a
    fraction of the pane width), bg, fg, mono, size_delta, weight, align, and
    whether content height is observed (rules are fixed-height).
    """
    kind = block.get("kind", PARAGRAPH)
    who = block.get("who")
    base = {"mono": False, "size_delta": 0, "weight": 100.0, "align": 0,
            "fg": TEXT, "bg": MODEL_BG, "observed": True}
    if kind == HEADING:
        base.update(weight=150.0, size_delta=3, gap_before=GAP_HEADING_BEFORE,
                    gap_after=GAP_HEADING_AFTER)
    elif kind == BULLET:
        base.update(prefix="\u2022 ")

    elif kind == CODE:
        base.update(mono=True, bg=CODE_BG, fg=CODE_TEXT,
                    gap_before=GAP_CODE, gap_after=GAP_CODE)

    elif kind == RULE:
        base.update(bg=RULE_BG, observed=False, fixed_h=RULE_HEIGHT)

    elif kind in (CENTER, CENTER_MUTED):
        base.update(align=1, bg=PANEL_BG)
        if kind == CENTER_MUTED:
            base["fg"] = MUTED
    elif who == "you":
        base.update(bg=USER_BG, fg=USER_TEXT, gap_before=GAP_MSG,
                    gap_after=GAP_MSG, pad_b=BUBBLE_PAD)
    else:
        base.update(gap_before=GAP_BLOCK, gap_after=GAP_BLOCK)
    base.setdefault("prefix", "")
    base.setdefault("gap_before", GAP_BLOCK)
    base.setdefault("gap_after", GAP_BLOCK)
    base.setdefault("pad_b", BLOCK_PAD)
    # Bubbles are mirrored fractions of the pane width (user 20%/5%, model
    # 5%/20%) — the user's spec, and what actually reads as a conversation.
    base["kind"] = kind
    base["who"] = who
    if kind in (CENTER, CENTER_MUTED):
        base["x_frac"] = 0.02
        base["right_frac"] = 0.02
    elif who == "you":
        base["x_frac"] = USER_X_FRAC
        base["right_frac"] = USER_RIGHT_FRAC
    else:
        base["x_frac"] = MODEL_X_FRAC
        base["right_frac"] = MODEL_RIGHT_FRAC
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
            # POROUS handoff: the planner must carry the full geometry the
            # renderer needs, or the apply pass silently re-inflates the
            # width. 'right' dropped here was the model bubble's missing
            # right-hand inset: measured at 75% width, drawn at ~95%.
            "right": row.get("right", 0.0),
            "who": row.get("who"),
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
