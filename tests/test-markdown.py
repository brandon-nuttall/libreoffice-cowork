#!/usr/bin/env python3
"""Tests for the markdown renderer.

`cowork_markdown` has no UNO imports precisely so this can run without
LibreOffice. The formatting it produces is the part most likely to be wrong in
ways a screenshot would only show by luck — a bullet left as "-", a heading left
with its hashes — so it is tested directly.

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


def kinds(blocks):
    return [b["kind"] for b in blocks]


def texts(blocks, kind=None):
    return [b["text"] for b in blocks if kind is None or b["kind"] == kind]


def main():
    print("markdown rendering\n")

    print("the shapes an agent actually replies with")
    sample = (
        "Hello! I'm Cowork — I work on the LibreOffice document you have open.\n"
        "\n"
        "A few things I can do:\n"
        "\n"
        "- **Edit or rewrite text** in a Writer doc\n"
        "- **Review a document** for layout problems\n"
        "- Build a presentation from an outline\n"
        "\n"
        "If you have something open, just tell me what you'd like changed.\n"
    )
    blocks = md.parse(sample)
    check("bullets are recognised, not left as dashes",
          kinds(blocks).count(md.BULLET) == 3, kinds(blocks))
    check("no bullet text keeps its leading dash",
          not any(t.startswith(("-", "*")) for t in texts(blocks, md.BULLET)),
          texts(blocks, md.BULLET))
    check("no bold markers survive anywhere",
          not any("**" in t for t in texts(blocks)), texts(blocks))
    check("paragraphs are separate blocks",
          kinds(blocks).count(md.PARAGRAPH) >= 2, kinds(blocks))

    print("\nheadings")
    blocks = md.parse("## What I found\n\nDetails here.")
    check("a heading becomes a heading block",
          kinds(blocks)[0] == md.HEADING, kinds(blocks))
    check("and keeps its level", blocks[0].get("level") == 2, blocks[0])
    check("with the hashes removed", blocks[0]["text"] == "What I found", blocks[0])

    print("\ncode fences")
    blocks = md.parse("Run this:\n\n```\nsoffice --headless\n```\n\nDone.")
    check("a fenced block becomes a code block",
          md.CODE in kinds(blocks), kinds(blocks))
    check("the fence markers are gone",
          not any("```" in t for t in texts(blocks)), texts(blocks))
    check("the command survives intact",
          "soffice --headless" in texts(blocks, md.CODE), texts(blocks, md.CODE))

    print("\nunclosed fence")
    blocks = md.parse("text\n\n```\nstill code")
    check("an unterminated fence still yields a code block",
          md.CODE in kinds(blocks), kinds(blocks))

    print("\ninline emphasis")
    check("bold markers are stripped",
          md.strip_inline("**bold** and normal") == "bold and normal")
    check("italic markers are stripped",
          md.strip_inline("*italic* here") == "italic here")
    check("inline code backticks are stripped",
          md.strip_inline("run `ls` now") == "run ls now")
    check("link URLs are dropped but text kept",
          md.strip_inline("see [the docs](https://example.com)") == "see the docs")
    check("an underscore in a word is left alone",
          md.strip_inline("file_name here") == "file_name here")
    check("a lone asterisk is not treated as emphasis",
          md.strip_inline("2 * 3 = 6") == "2 * 3 = 6")

    print("\nordered lists")
    blocks = md.parse("1. first\n2. second")
    check("numbered items become bullets with their number kept",
          texts(blocks, md.BULLET) == ["1. first", "2. second"], texts(blocks, md.BULLET))

    print("\nthe conversation wrapper")
    blocks = md.layout([
        {"kind": "cowork", "text": "Hello there."},
        {"kind": "you", "text": "Do the thing"},
        {"kind": "cowork", "text": "Done."},
    ])
    check("each message gets a speaker label",
          texts(blocks, md.SPEAKER) == ["Cowork", "You", "Cowork"],
          texts(blocks, md.SPEAKER))
    check("labels are separate blocks from the text",
          kinds(blocks)[0] == md.SPEAKER and kinds(blocks)[1] == md.PARAGRAPH,
          kinds(blocks))

    print("\nspacing")
    blocks = md.layout([
        {"kind": "cowork", "text": "First message."},
        {"kind": "you", "text": "Second message."},
    ])
    gaps = [md.gap_before(b, blocks[i - 1] if i else None)
            for i, b in enumerate(blocks)]
    check("a new message has more space above it than a continuation",
          max(gaps) >= md.GAP_BLOCK, gaps)
    check("the first block has no leading gap", gaps[0] == 0, gaps)

    print("\nhostile input")
    for label, hostile in [
        ("empty string", ""),
        ("only newlines", "\n\n\n"),
        ("only markers", "***"),
        ("unbalanced bold", "**unclosed"),
        ("unbalanced backticks", "`unclosed"),
        ("a very long line", "word " * 500),
    ]:
        try:
            md.parse(hostile)
            check("%s does not crash" % label, True)
        except Exception as exc:  # noqa: BLE001
            check("%s does not crash" % label, False, exc)

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All markdown checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
