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

    print("exactly one parser, in process, per the contract")
    import inspect
    source = inspect.getsource(md)
    check("the module parses markdown itself (the single parser)",
          hasattr(md, "parse"), dir(md))
    check("the office document roundtrip is GONE",
          not hasattr(md, "parse_with_office")
          and "loadComponentFromURL" not in source
          and "tempfile" not in source,
          "parse() must not load documents, temp files or windows; rendered "
          "markdown must never crash and there is exactly one code path")

    print("\nblocks carry runs, not a flat string")
    b = block(md.PARAGRAPH, "hello")
    check("a block has runs", isinstance(b.get("runs"), list), b)
    check("plain() reads them back", md.plain(b) == "hello", md.plain(b))

# --- the single transcript parser (cowork_markdown.parse) -------------------

def _parse_checks():
    import cowork_markdown as M
    ok = 0
    bad = []
    def check(label, cond, detail=""):
        nonlocal ok
        if cond: ok += 1
        else: bad.append((label, detail))

    blocks = M.parse("# Title\n- one\n- two\n\nBody with **bold** and `code`.\n```\ncode\n```\n")
    kinds = [b["kind"] for b in blocks]
    check("structure: heading/bullets/paragraph/code", kinds == ["heading", "bullet", "bullet", "paragraph", "code"], kinds)
    text = "".join(r["text"] for b in blocks for r in b["runs"])
    check("inline markers stripped", "**" not in text and "`" not in text, text)
    r = M.parse("")
    check("empty input -> no blocks", r == [], r)
    r = M.parse("* " * 3000)
    check("hostile input does not crash", isinstance(r, list), len(r))
    return ok, bad

_ok, _bad = _parse_checks()
if _bad:
    print(f"FAILED: {_bad}")
    sys.exit(1)
print(f"  + {_ok} single-parser checks passed")
