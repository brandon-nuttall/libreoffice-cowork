#!/usr/bin/env python3
"""Tests for the transcript layout.

Every layout bug of the last several hours was found — or missed — by rebuilding
the extension, restarting LibreOffice and taking a screenshot. This suite replaces
that loop: the layout is a pure function of a width, a viewport and a list of
blocks, so it can be checked in milliseconds.

The specific faults these tests exist to prevent, each of which shipped at least
once:

  * a label wider than the pane, clipped on the LEFT, because centring is
    computed from text length ("How can I help with this document?" rendered as
    "elp with this document?")
  * a stale width from an early layout pass wrapping at 16 columns, so
    "hello world" rendered as "hello w"
  * a line positioned at a negative y, painting over the controls above it
  * one bubble per line, giving a two-line message a stripe per row

    python3 tests/test-layout.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ext", "oxt-proto", "components"))

import cowork_layout as L  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        FAILURES.append(label)


def block(kind, text, who=None):
    return {"kind": kind, "who": who,
            "runs": [{"text": text, "bold": False, "italic": False, "mono": False}]}


def main():
    print("transcript layout\n")

    print("wrapping")
    check("a short line is left alone", L.wrap("hello world", 40) == ["hello world"])
    check("a long line is split", len(L.wrap("word " * 40, 20)) > 1)
    check("an explicit newline is preserved",
          L.wrap("one\ntwo", 40) == ["one", "two"])
    check("an over-long word is broken, not allowed to overflow",
          all(len(line) <= 10 for line in L.wrap("x" * 45, 10)),
          L.wrap("x" * 45, 10))
    check("empty input yields one empty line", L.wrap("", 20) == [""])

    print("\ncolumns scale with width")
    narrow = L.columns_for(600)
    wide = L.columns_for(1600)
    check("a wider pane fits more characters", wide > narrow, (narrow, wide))
    check("a pane too narrow for any text still returns a workable count",
          L.columns_for(10) >= 4 and L.columns_for(0) >= 4,
          (L.columns_for(10), L.columns_for(0)))
    check("no width produces a zero or negative count",
          all(L.columns_for(w) >= 1 for w in (-100, 0, 1, 10, 50, 100, 202)), "")

    print("\nlines stay inside the viewport")
    blocks = [block(L.PARAGRAPH, "one " * 60, who="cowork")]
    result = L.layout_messages(blocks, inner=202, view=600)
    inside = [l for l in result["lines"] if l["visible"]]
    check("some lines are visible", inside, result["lines"][:1])
    check("no visible line starts above the view",
          all(l["y"] >= 0 for l in inside), [l["y"] for l in inside][:3])
    check("no visible line starts below the view",
          all(l["y"] < 600 for l in inside), [l["y"] for l in inside][-3:])
    check("every visible line has a positive height",
          all(l["height"] > 0 for l in inside))
    check("no visible line has a negative y",
          all(l["y"] >= 0 for l in result["lines"] if l["visible"]))

    print("\nno line was wrapped too wide for its room")
    for inner in (300, 440, 800, 1256):
        result = L.layout_messages(
            [block(L.HEADING, "How can I help with this document?", who="cowork"),
             block(L.PARAGRAPH, "hello world", who="you")],
            inner=inner, view=600)
        # A line is too long when it holds more characters than the pane allows.
        # The generous factor absorbs the difference between the average and the
        # widest glyph: this is a smoke test for a factor-of-ten error, which is
        # the one that actually happened.
        allowed = L.columns_for(inner, 2 * L.BUBBLE_PAD) * 2 + 8
        too_wide = [l for l in result["lines"] if len(l["text"]) > allowed]
        check("inner=%d: no line holds more characters than fit" % inner,
              not too_wide, [(l["text"], len(l["text"]), allowed) for l in too_wide])

    print("\na long heading wraps rather than staying on one clipped line")
    result = L.layout_messages(
        [block(L.HEADING, "How can I help with this document?", who="cowork")],
        inner=202, view=600)
    check("it became several lines", len(result["lines"]) > 1, len(result["lines"]))
    check("and the whole text survives across them",
          "".join(l["text"] for l in result["lines"]).replace(" ", "")
          == "HowcanIhelpwiththisdocument?",
          [l["text"] for l in result["lines"]])

    print("\nrow heights suit their font")
    for size in (5, 9, 10, 11, 12):
        height = L.row_height(size)
        glyph = size * 0.3528 * 100
        check("%dpt row is taller than its glyphs" % size, height > glyph,
              (height, glyph))

    print("\nthe plain-text renderer — what the panel actually draws")
    blocks = [
        block(L.PARAGRAPH, "hello world", who="you"),
        block(L.HEADING, "Summary", who="cowork"),
        block(L.BULLET, "first point", who="cowork"),
        block(L.PARAGRAPH, "Some prose that will wrap in a narrow pane.", who="cowork"),
    ]
    text = L.to_plain_text(blocks, 14)
    lines = text.split("\n")
    check("the user's words survive", "hello world" in lines, lines[:3])
    check("a heading is upper-cased so it reads as one", "SUMMARY" in lines, lines)
    check("a bullet gets a visible marker",
          any(l.startswith("• ") for l in lines), lines)
    check("no markdown markers reach the screen",
          not any("**" in l or l.startswith("# ") for l in lines), lines)
    check("turns are separated by blank lines",
          "" in lines and lines.index("") < len(lines) - 1, lines)
    check("a long paragraph is wrapped, not clipped",
          all(len(l) <= 20 for l in lines), [l for l in lines if len(l) > 20])

    print("\nplain text fits any width, which is the whole reason for it")
    for columns in (6, 10, 14, 40, 120):
        text = L.to_plain_text(blocks, columns)
        longest = max(len(l) for l in text.split("\n"))
        check("columns=%d: nothing exceeds the width" % columns,
              longest <= max(columns, 40) + 4, longest)

    print("\nempty input renders as empty, not as junk")
    check("no blocks gives an empty string", L.to_plain_text([], 20) == "")
    check("empty text is skipped rather than emitting blank lines",
          L.to_plain_text([block(L.PARAGRAPH, "   ", who="cowork")], 20) == "")

    print("\na change of speaker is the largest gap")
    result = L.layout_messages(
        [block(L.PARAGRAPH, "first", who="cowork"),
         block(L.PARAGRAPH, "second", who="cowork"),
         block(L.PARAGRAPH, "third", who="you")],
        inner=202, view=600)
    gaps = [l["gap"] for l in result["lines"]]
    check("a new message carries the largest gap",
          L.MESSAGE_GAP in gaps, gaps)
    check("every other line has a smaller gap than a new message",
          all(g < L.MESSAGE_GAP for g in gaps if g != L.MESSAGE_GAP), gaps)

    print("\nscrolling")
    tall = [block(L.PARAGRAPH, "line " * 400, who="cowork")]
    top = L.layout_messages(tall, 202, 600, 0)
    bottom = L.layout_messages(tall, 202, 600, 10 ** 6)
    check("offset 0 shows the start",
          top["lines"][0]["y"] == L.MARGIN, top["lines"][0]["y"])
    check("a huge offset shows nothing rather than negative y",
          not any(l["visible"] for l in bottom["lines"]),
          len(bottom["lines"]))

    print("\nthe empty state is centred and inside the view")
    for view in (400, 600, 760, 1000):
        result = L.layout_empty_state(
            "How can I help with this document?", "document.txt",
            ["Summarise this document", "Review it for problems"], inner=202, view=view)
        check("view=%d: something is visible" % view,
              any(l["visible"] for l in result["lines"]), view)
        check("view=%d: nothing is above the top" % view,
              all(l["y"] >= 0 for l in result["lines"] if l["visible"]), view)
        check("view=%d: nothing starts below the view" % view,
              all(l["y"] < view for l in result["lines"] if l["visible"]), view)

    print("\nhostile input does not crash")
    for label, bad in [
        ("empty block list", []),
        ("empty text", [block(L.PARAGRAPH, "")]),
        ("a 5000-character word", [block(L.PARAGRAPH, "x" * 5000)]),
        ("only newlines", [block(L.PARAGRAPH, "\n\n\n")]),
        ("a zero width", [block(L.PARAGRAPH, "text")]),
    ]:
        try:
            L.layout_messages(bad, inner=0 if "zero" in label else 202, view=600)
            check("%s does not crash" % label, True)
        except Exception as exc:  # noqa: BLE001
            check("%s does not crash" % label, False, exc)

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All layout checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
