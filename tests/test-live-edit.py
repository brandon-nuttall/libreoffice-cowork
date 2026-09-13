#!/usr/bin/env python3
"""Prove the project's core promise: the agent edits the LIVE document, and the
whole turn is a single undo step.

This is the claim the project is built on, and the one a user tests within sixty
seconds of opening it. It has been exercised by hand many times but never by a
test, which means every panel change could have broken it silently.

What it checks, in order:

  1. the agent can see the document the user has open
  2. a multi-edit request actually changes the live document
  3. the change is visible to a separate reader of the same live document —
     i.e. it is the document on screen, not a copy somewhere
  4. the whole turn is ONE undo step, not one per edit
  5. undo restores the document exactly, including anything that was there before
  6. the pre-existing content survives every step

Needs a running office with the extension (the sandbox is ideal) and a live
runtime. In the sandbox:

    ./tests/sandbox-ui.sh start
    python3 tests/test-live-edit.py

Exit code is non-zero if any check fails.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = os.path.join(ROOT, "dsh", "libreoffice", "cowork", "uno_bridge.py")

sys.path.insert(0, os.path.join(ROOT, "ext", "oxt-proto", "components"))

from cowork_client import AgentClient, AgentUnavailable  # noqa: E402

FAILURES = []
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "240"))

MARKER_BEFORE = "KEEPME"


class Bridge:
    """The document bridge, driven directly so the test sees the live document."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, BRIDGE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=dict(os.environ))

    def call(self, op, **args):
        self.proc.stdin.write(json.dumps({"id": op, "op": op, "args": args}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the bridge produced no answer to %r" % op)
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        FAILURES.append(label)


def main():
    print("live editing and one-undo turns\n")

    bridge = Bridge()
    try:
        status = bridge.call("ping")
        if not status.get("ok"):
            print("The bridge cannot reach LibreOffice: %s" % status.get("error"))
            print("Start an office with the extension, e.g. ./tests/sandbox-ui.sh start")
            return 1

        documents = bridge.call("list_documents")
        check("the agent can see an open document",
              bool(documents.get("documents")), documents)
        if not documents.get("documents"):
            return 1
        document = documents["documents"][0]
        print("      document: %s" % document["title"])

        print("\nmake sure there is pre-existing content to preserve")
        before = bridge.call("read_text")
        if MARKER_BEFORE not in before.get("text", ""):
            bridge.call("begin_turn", turn_id="setup", undo_title="test setup")
            bridge.call("append", text="\n%s existing line." % MARKER_BEFORE, turn_id="setup")
            bridge.call("end_turn", turn_id="setup")
            before = bridge.call("read_text")
        original = before.get("text", "")
        check("the document has the marker before we start", MARKER_BEFORE in original,
              repr(original[-80:]))
        depth_before = bridge.call("undo_state")["undo"]["undoDepth"]

        print("\nask the agent to make several edits in one turn")
        prompt = (
            "In the open document, append two short paragraphs. The first should say "
            "'AGENTEDITALPHA'. The second should say 'AGENTEDITBETA'. Then finish the "
            "editing turn so the whole change is a single undo step."
        )
        client = AgentClient()
        if not client.ping().get("runtime"):
            print("The Cowork runtime is not running; start the panel once, or run")
            print("  dsh/libreoffice/cowork/cowork-runtime.sh")
            return 1

        try:
            final = client.ask(document["url"], prompt, lambda kind, payload: None)
        except AgentUnavailable as exc:
            check("the agent completes the request", False, exc)
            return 1
        check("the agent answered", bool((final or "").strip()), repr(final[:120]))

        print("\nthe LIVE document changed")
        after = bridge.call("read_text")
        text = after.get("text", "")
        check("the first edit landed", "AGENTEDITALPHA" in text,
              repr(text[-160:]))
        check("the second edit landed", "AGENTEDITBETA" in text,
              repr(text[-160:]))
        check("content that was already there survived",
              MARKER_BEFORE in text, repr(text[:160]))

        print("\nthe whole turn is ONE undo step")
        state = bridge.call("undo_state")["undo"]
        print("      undo depth before: %s, after: %s, current action: %r"
              % (depth_before, state.get("undoDepth"), state.get("currentUndoAction")))
        grew = (state.get("undoDepth") or 0) - (depth_before or 0)
        check("the turn added ONE undo entry, not one per edit", grew == 1,
              "depth went from %s to %s" % (depth_before, state.get("undoDepth")))

        print("\nundo restores the document exactly")
        bridge.call("undo")
        restored = bridge.call("read_text").get("text", "")
        check("both agent edits are gone",
              "AGENTEDITALPHA" not in restored and "AGENTEDITBETA" not in restored,
              repr(restored[-160:]))
        check("the document matches what it was before the turn",
              restored.strip() == original.strip(),
              "before=%r after=%r" % (original[-60:], restored[-60:]))
        check("the pre-existing content is intact", MARKER_BEFORE in restored)

        print()
        if FAILURES:
            print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
            return 1
        print("Live editing works, and a turn is one undo step.")
        return 0
    finally:
        bridge.close()


if __name__ == "__main__":
    sys.exit(main())
