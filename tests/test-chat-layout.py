#!/usr/bin/env python3
"""Unit tests for the chat layout core (cowork_chat).

The renderer observes heights from the toolkit (probes C2/C3); this module owns
everything after that: gaps, stacking, viewport cutting, scrollbar maths and the
autoscroll target.äsenti

    python3 tests/test-chat-layout.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ext", "oxt-proto", "components"))

import cowork_chat as chat  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        FAILURES.append(label)


def row(key, h, kind="paragraph", who="cowork", gap=None):
    r = {"key": key, "h": h, "x": 0, "width_in": chat.EDGE,
         "kind": kind, "who": who}
    if gap is not None:
        r["gap"] = gap
    return r


def main():
    print("chat layout core\n")

    print("styles")
    user = chat.style_for({"kind": chat.PARAGRAPH, "who": "you"})
    reply = chat.style_for({"kind": chat.PARAGRAPH, "who": "cowork"})
    head = chat.style_for({"kind": chat.HEADING, "who": "cowork"})
    code = chat.style_for({"kind": chat.CODE, "who": "cowork"})
    check("the user's bubble is indented; the assistant's is not",
          user["x"] > reply["x"], (user["x"], reply["x"]))
    check("only the user gets the bubble background",
          user["bg"] == chat.USER_BG and reply["bg"] == chat.PLAIN_BG
          and head["bg"] == chat.PLAIN_BG, (user["bg"], reply["bg"]))
    check("headings are bold and larger", head["weight"] > 100
          and head["size_delta"] > 0)
    check("code is monospace on the code background",
          code["mono"] and code["bg"] == chat.CODE_BG)

    print("\ngaps")
    check("a new message gets the message gap",
          chat.gap_between(row("a", 10), row("b", 10))),  # noqa
    check("a speaker change is a bigger gap than a same-speaker block",
          chat.gap_between(row("a", 10, who="you"), row("b", 10, who="cowork"))
          > chat.gap_between(row("a", 10, who="cowork"), row("b", 10, who="cowork")))

    print("\nplan: stacking")
    r = chat.plan([row("a", 100), row("b", 100, gap=50)], view=1000)
    check("first row starts at 0", r["rows"][0]["y"] == 0)
    check("second row clears the first plus its gap", r["rows"][1]["y"] == 150,
          r["rows"][1]["y"])
    check("total is the sum", r["total"] == 250, r["total"])

    print("\nplan: cutting to the viewport")
    rows = [row("a", 100), row("b", 100), row("c", 100)]
    r = chat.plan(rows, view=250)
    check("rows inside the view are visible",
          r["rows"][0]["visible"] and r["rows"][1]["visible"])
    # At offset 0 nothing is above the window: a row starting past the view is
    # hidden. (Autoscroll pins the OTHER way — bottom visible, top hidden.)
    below = chat.plan([row("a", 100), row("b", 100), row("c", 100),
                       row("d", 100)], view=250, offset=0)
    check("rows entirely past the view are hidden",
          below["rows"][3]["visible"] is False, below["rows"][3])
    r2 = chat.plan(rows, view=150, offset=150)
    check("scrolling moves the used offset", r2["used_offset"] == 150,
          r2["used_offset"])
    check("and the row straddling the new window is visible",
          r2["rows"][2]["visible"], r2["rows"][2])

    print("\nplan: scrollbar")
    check("max_offset never negative", chat.plan(rows, view=1000)["max_offset"] == 0)
    check("max_offset = total - view", chat.plan(rows, view=150)["max_offset"] == 150)
    check("autoscroll pins to the bottom",
          chat.plan(rows, view=150, offset=None)["used_offset"] == 150)
    check("a user offset is clamped into range",
          chat.plan(rows, view=150, offset=999)["used_offset"] == 150)

    print("\nempty state rides the same pipeline")
    blocks = [{"kind": chat.CENTER, "who": "cowork",
               "runs": [{"text": "How can I help with this document?"}]},
              {"kind": chat.CENTER_MUTED, "who": "cowork",
               "runs": [{"text": "document.txt"}]}]
    check("centre blocks are centred",
          all(chat.style_for(b)["align"] == 1 for b in blocks))
    check("the muted subtitle is muted",
          chat.style_for(blocks[1])["fg"] == chat.MUTED)

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All chat-layout checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
