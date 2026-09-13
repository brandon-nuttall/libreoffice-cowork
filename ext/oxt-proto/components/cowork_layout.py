"""Transcript layout as a pure function.

WHY THIS FILE EXISTS
--------------------
The panel's layout was tangled into the control code, so the only way to find out
what it did was to rebuild the extension, restart LibreOffice, activate the deck
and take a screenshot — about a minute per attempt. Every layout bug of the last
several hours was found that way, and several were not found at all:

  * a heading that wrapped to a single unwrapped centred label, so it clipped
    "How can I help" to "elp with this document?"
  * a cached width from an early layout pass that wrapped every message at 16
    columns, rendering "hello world" as "hello w"
  * an empty state that flowed into the transcript and left two words far apart
    down a blank panel
  * rows positioned below a viewport they were measured against

Not one of those is a visual question. Each is: given a width, a viewport and a
list of blocks, where does every line go, and is it inside the view? That is a
pure function of numbers, and numbers can be tested in milliseconds.

So this module computes the geometry and nothing else. It imports no UNO, takes no
context, and touches no control. The panel asks it what to draw and then draws it.

Units are the container's: 1/100 mm, y increasing downward.
"""

import math

# Block kinds, mirroring cowork_markdown's, kept as strings so this module does
# not depend on the parser either.
HEADING = "heading"
PARAGRAPH = "paragraph"
BULLET = "bullet"
CODE = "code"
RULE = "rule"
SPEAKER = "speaker"

# Appearance, in points.
STYLE = {
    SPEAKER:   {"weight": 150.0, "size": 9,  "mono": False, "indent": 0},
    HEADING:   {"weight": 150.0, "size": 11, "mono": False, "indent": 0},
    PARAGRAPH: {"weight": 100.0, "size": 10, "mono": False, "indent": 0},
    BULLET:    {"weight": 100.0, "size": 10, "mono": False, "indent": 12},
    CODE:      {"weight": 100.0, "size": 9,  "mono": True,  "indent": 12},
    RULE:      {"weight": 100.0, "size": 8,  "mono": False, "indent": 0},
}

# Vertical gaps, in the container's units.
GAP_TIGHT = 20
GAP_NORMAL = 90
GAP_BLOCK = 160
MESSAGE_GAP = 200

MARGIN = 6
# Width of one AVERAGE character at 10pt in the UI font, in the container's units
# (1/100 mm). Noto Sans averages about 1.5mm per character; 1.8mm leaves room for
# capitals and wide glyphs without wasting a line.
#
# An earlier value was 190 "mm/100" -- that is 1.9 MILLIMETRES read as if it were
# 1.9 units, a factor-of-a-hundred error... which then got clamped by the minimum
# and quietly produced one column per line. A label clips silently rather than
# overflowing visibly, so this number being too small is safe and being too large
# is not.
CHAR_WIDTH = 180
# Padding either side of a user message's bubble.
BUBBLE_PAD = 30
# Below this much usable width a bubble costs more than it communicates: its
# padding leaves too few characters per line to read. LibreOffice's sidebar is
# 232 units wide by default, so this threshold is met only when the user has
# widened it.
BUBBLE_MIN_INNER = 420


def row_height(point_size):
    """Height for one line at a point size, in 1/100 mm.

    10pt needs roughly 3.5mm of glyph plus leading. Derived rather than fixed: a
    single constant cannot fit a 9pt caption and a 12pt heading, and both failure
    modes are invisible in code — too small and lines overlap, too large and every
    message is padded with dead space.
    """
    return int((point_size * 0.3528 + 2.0) * 100)


def wrap(text, columns):
    """Hard-wrap to a character grid, preserving explicit newlines.

    A long word is broken rather than allowed to overflow, because a label cannot
    wrap itself and an over-wide centred label is clipped on the LEFT.
    """
    columns = max(columns, 8)
    out = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = ""
        for word in paragraph.split(" "):
            candidate = word if not line else line + " " + word
            if len(candidate) <= columns:
                line = candidate
                continue
            if line:
                out.append(line)
            while len(word) > columns:
                out.append(word[:columns])
                word = word[columns:]
            line = word
        if line:
            out.append(line)
    return out or [""]


def columns_for(inner, extra_inset=0, keep_room=True):
    """How many characters fit, after reserving room to breathe.

    `extra_inset` is padding the caller's styling adds — a user message sits
    inside a bubble, so its text has less room than the pane.

    A fixed minimum is deliberately NOT applied. An early version forced at least
    12 columns, so a narrow pane produced lines wider than the pane and they were
    clipped; the whole point of the number is that it is derived from the space
    actually available.
    """
    usable = inner - 2 * MARGIN - extra_inset
    if usable <= 0:
        return 4
    return max(int(usable / CHAR_WIDTH), 4)


def layout_messages(blocks, inner, view, offset=0, bubbles=True):
    """Geometry for a conversation.

    Returns `{"lines": [...], "bubbles": [...], "total": int}` where each line
    carries its own y, height, x, width, text and styling, and each bubble carries
    the y and height covering one user message.

    Every returned line is inside `[0, view)` — lines outside are dropped rather
    than positioned, because a label at a negative y paints over the controls
    above it.
    """
    # A user message is inset only when it is actually getting a bubble.
    columns = columns_for(inner, 2 * BUBBLE_PAD if bubbles else 0)
    # Shrink until the estimate is actually satisfied. Wrapping by character count
    # is approximate, and a label clips silently rather than overflowing visibly,
    # so the estimate is checked here rather than trusted.
    for _ in range(12):
        widest = 0
        for block in blocks:
            kind = block.get("kind", PARAGRAPH)
            style = STYLE.get(kind, STYLE[PARAGRAPH])
            inset = MARGIN + (BUBBLE_PAD if (bubbles and block.get("who") == "you")
                              else style["indent"])
            if bubbles and block.get("who") == "you":
                inset += BUBBLE_PAD          # the bubble's right padding too
            text = "".join(run.get("text", "") for run in block.get("runs", []))
            for line in wrap(text, columns - (2 if style["indent"] else 0)):
                widest = max(widest, inset + len(line) * CHAR_WIDTH)
        if widest <= inner or columns <= 1:
            break
        columns -= 1

    rows = []
    previous = None
    for block in blocks:
        kind = block.get("kind", PARAGRAPH)
        style = STYLE.get(kind, STYLE[PARAGRAPH])
        text = "".join(run.get("text", "") for run in block.get("runs", []))
        if kind == RULE:
            text = "─" * min(columns, 40)

        gap = gap_before(kind, previous, block.get("who"), previous_who(previous))
        available = columns - (2 if style["indent"] else 0)
        first = True
        for line in wrap(text, available):
            rows.append({
                "who": block.get("who"),
                "kind": kind,
                "text": line,
                "style": style,
                # The block's gap rides on its first line only.
                "gap": gap if first else 0,
                "height": row_height(style["size"]),
            })
            first = False
        previous = block

    # One bubble per contiguous run of user lines, not one per line: a per-line
    # bubble gave a two-line message a stripe per row.
    bubbles = []
    y = MARGIN - offset
    lines = []
    for row in rows:
        y += row["gap"]
        is_user = row["who"] == "you" and bubbles
        indent = MARGIN + (BUBBLE_PAD if is_user else row["style"]["indent"])
        width = max(inner - (indent - MARGIN), 40)
        inside = y + row["height"] > 0 and y < view
        lines.append({
            "y": y, "x": indent, "width": width, "height": row["height"],
            "gap": row["gap"],
            "text": row["text"], "style": row["style"], "kind": row["kind"],
            "who": row["who"], "visible": inside,
        })
        if is_user:
            if bubbles and bubbles[-1]["open"] and bubbles[-1]["who"] == "you":
                last = bubbles[-1]
                last["height"] = (y + row["height"]) - last["y"] + 48
            else:
                bubbles.append({"who": "you", "y": y - 24, "x": MARGIN,
                                "width": inner, "height": row["height"] + 48,
                                "open": True,
                                "visible": (y - 24) < view})
        else:
            if bubbles:
                bubbles[-1]["open"] = False
        y += row["height"]

    total = MARGIN + sum(r["gap"] + r["height"] for r in rows) + MARGIN
    for bubble in bubbles:
        bubble.pop("open", None)
    return {"lines": lines, "bubbles": bubbles, "total": total, "columns": columns}


def previous_who(previous):
    return (previous or {}).get("who") if previous else None


def gap_before(kind, previous, who, last_who):
    if previous is None:
        return 0
    # A change of speaker is the strongest visual break in a conversation.
    if who != last_who:
        return MESSAGE_GAP
    if kind in (HEADING, RULE, CODE):
        return GAP_NORMAL
    return GAP_TIGHT


def layout_empty_state(title, subtitle, suggestions, inner, view):
    """Geometry for the opening view, centred vertically.

    Centred rather than flowed from the top: flowing it put the heading at y=934
    inside a 607-unit pane, so the pane looked empty; anchoring the chips to the
    foot with `view - chips - 120` put every one below the fold instead. Centring
    needs no knowledge of the content's height beyond its own total, and is
    correct at any pane height.
    """
    columns = columns_for(inner)
    block = []
    for text, size, weight, colour in (
            [(line, 12, 150.0, -1) for line in wrap(title, columns)]
            + [(line, 9, 100.0, "muted") for line in wrap(subtitle, columns)]
            + [("", 5, 100.0, -1)]):
        block.append({"text": text, "size": size, "weight": weight,
                      "colour": colour, "height": row_height(size)})
    for suggestion in suggestions:
        for line in wrap(suggestion, columns):
            block.append({"text": line, "size": 10, "weight": 100.0,
                          "colour": -1, "height": row_height(10)})

    total = sum(spec["height"] + 40 for spec in block)
    y = max(int((view - total) / 2), MARGIN)
    for spec in block:
        spec["y"] = y
        spec["x"] = MARGIN
        spec["width"] = max(inner, 40)
        spec["centre"] = True
        spec["visible"] = y + spec["height"] > 0 and y < view
        y += spec["height"] + 40
    return {"lines": block, "bubbles": [], "total": view}


def to_plain_text(blocks, columns):
    """Render blocks as readable plain text for a single text control.

    THIS IS THE DESIGN, and it was arrived at the hard way. An earlier version drew
    the conversation as a stack of individually-positioned labels so it could have
    per-run styling and chat bubbles. That works on paper and fails here, for a
    reason no amount of layout care fixes:

    **LibreOffice's sidebar produces a panel 2.32 cm wide** — 232 units in this
    toolkit's 1/100 mm, about 88 px, and roughly fourteen characters per line at
    10pt. A bubble's padding leaves six. Bubbles, right-alignment and per-line
    styling are all fine ideas that cannot fit, which is exactly why Claude for
    Word uses a single plain text pane and not bubbles.

    A single text control also does the two things the label stack kept getting
    wrong: it wraps natively at any width, and it scrolls natively.

    Emphasis is dropped rather than lost: the parser has already removed the
    markers, so "**bold**" arrives as "bold". Headings become their text on their
    own line, bullets become "•", code is indented, and rules become a line of
    dashes.
    """
    out = []
    previous_who = None
    for block in blocks:
        kind = block.get("kind", PARAGRAPH)
        who = block.get("who")
        text = "".join(run.get("text", "") for run in block.get("runs", [])).strip()
        if not text:
            continue

        if who != previous_who and previous_who is not None:
            out.append("")                      # a blank line between messages
            out.append("")                      # and one more, so turns are clear

        if kind == HEADING:
            out.append(text.upper() if len(text) < 60 else text)
        elif kind == BULLET:
            for line in wrap(text, columns - 2):
                out.append("• " + line)
        elif kind == CODE:
            for line in text.split("\n"):
                out.append("    " + line)
        elif kind == RULE:
            out.append("-" * min(columns, 30))
        else:
            out.extend(wrap(text, columns))
        previous_who = who
    return "\n".join(out)
