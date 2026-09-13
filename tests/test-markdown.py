#!/usr/bin/env python3
"""Tests for the transcript block model.

Markdown PARSING is no longer ours — LibreOffice's own Markdown filter does it —
so there is nothing to test here about syntax. What remains testable without a
live office is the part we still own: the block model, the layout maths, and the
emphasis thresholds that decide whether a text run is bold.

An earlier version of this file tested a regex parser. That parser was deleted
when the office filter replaced it, and these tests were rewritten rather than
left passing against code that no longer exists.

    python3 tests/test-markdown.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ext", "oxt-proto", "components"))

import cowork_markdown as md  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        FAILURES.append(label)


def block(kind, text, bold=False):
    return {"kind": kind, "runs": [{"text": text, "bold": bold,
                                    "italic": False, "mono": False}]}


def main():
    print("transcript block model\n")

    print("the module does not parse markdown itself")
    check("there is no hand-written parser left",
          not hasattr(md, "parse") and not hasattr(md, "strip_inline"),
          "a parser reappeared; the office filter is supposed to own this")
    check("parsing is delegated to the office filter",
          hasattr(md, "parse_with_office"), dir(md))

    print("\nblocks carry runs, not a flat string")
    b = block(md.PARAGRAPH, "hello")
    check("a block has runs", isinstance(b.get("runs"), list), b)
    check("plain() reads them back", md.plain(b) == "hello", md.plain(b))

    print("\nspeaker labels come from the entry kind")
    blocks = md.layout([
        {"kind": "cowork", "text": "one"},
        {"kind": "you", "text": "two"},
    ], lambda text: [block(md.PARAGRAPH, text)])
    speakers = [md.plain(x) for x in blocks if x["kind"] == md.SPEAKER]
    check("each message gets a label", speakers == ["Cowork", "You"], speakers)
    check("labels are separate from the body",
          [x["kind"] for x in blocks] ==
          [md.SPEAKER, md.PARAGRAPH, md.SPEAKER, md.PARAGRAPH],
          [x["kind"] for x in blocks])

    print("\na parse failure degrades instead of crashing")
    def failing(_text):
        raise RuntimeError("no office")
    try:
        md.layout([{"kind": "cowork", "text": "x"}], failing)
        check("layout propagates the failure for the caller to handle", False,
              "it swallowed the error")
    except RuntimeError:
        check("layout propagates the failure for the caller to handle", True)

    print("\nspacing")
    blocks = md.layout([
        {"kind": "cowork", "text": "first"},
        {"kind": "you", "text": "second"},
    ], lambda text: [block(md.PARAGRAPH, text)])
    gaps = [md.gap_before(b, blocks[i - 1] if i else None) for i, b in enumerate(blocks)]
    check("a new message is separated more than a continuation",
          max(gaps) >= md.GAP_BLOCK, gaps)
    check("the first block has no leading gap", gaps[0] == 0, gaps)

    print("\nthe style maps use the filter's own style names")
    check("heading styles are mapped from the filter's own names",
          md._HEADING_STYLES.get("Heading 1") == 1, md._HEADING_STYLES)
    check("code styles are mapped from the filter's own names",
          "Preformatted Text" in md._CODE_STYLES, md._CODE_STYLES)

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All block-model checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
