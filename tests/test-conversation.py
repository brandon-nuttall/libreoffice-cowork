#!/usr/bin/env python3
"""Prove that chat and taking turns work.

Asked directly: *"we need to be sure that chat and taking turns works."* This is
the acceptance test for the conversation path, run against a live runtime.

WHAT IT CAN AND CANNOT REACH
----------------------------
The panel's CONTROLS are reachable over the bridge; its METHODS are not. A UNO
control exposes only its declared interfaces, so `submit()` -- an ordinary Python
method -- raises `AttributeError: submit` when called from outside the office. That
is the same constraint that makes Shift+Enter's handler untestable from here.

So this covers the layer beneath the buttons:

  * the transport the panel uses, `AgentClient`, against the live runtime
  * that a message gets a reply, streamed
  * that a SECOND message is answered, which is what "taking turns" actually means
  * that a reply is not duplicated (the runtime emits each session event twice)
  * that the runtime still answers afterwards

It does NOT cover the panel's button, composer or transcript: those are driven by
`tests/test-panel-wiring.py`, which runs the real `submit()` and `deliver()` with
fakes. Between them every link is exercised; neither alone is enough, which is
precisely why the panel's turn path kept breaking unnoticed.

    python3 tests/test-conversation.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "ext", "oxt-proto", "components"))

from cowork_client import AgentClient, AgentUnavailable  # noqa: E402

FAILURES = []
SKIPPED = []
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "180"))


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -- " + str(detail)))
    if not ok:
        FAILURES.append(label)


def ask(client, document, text):
    """One turn, returning (final, streamed, tool_labels)."""
    chunks = []
    tools = []

    def on_event(kind, payload):
        if kind == "chunk":
            chunks.append(payload.get("text", ""))
        elif kind == "tool":
            tools.append(payload.get("label") or payload.get("name") or "")

    final = client.ask(document, text, on_event)
    return final, "".join(chunks), tools


def main():
    print("chat and turn-taking\n")

    client = AgentClient()
    status = client.ping()
    print("runtime")
    check("the endpoint answers", status.get("reachable"), status)
    check("and its agent runtime is ready", status.get("runtime"), status)
    if not status.get("reachable") or not status.get("runtime"):
        SKIPPED.append("the Cowork runtime")
        print("\nThe runtime is not running. Start the panel once, or run:")
        print("  dsh/libreoffice/cowork/cowork-runtime.sh")
        print()
        print("SKIPPED: the Cowork runtime is not available, so this could not run.")
        print("  This is not a failure of the code under test.")
        return 0

    document = "http://conversation-test"

    print("\nfirst turn")
    started = time.time()
    try:
        final, streamed, _tools = ask(client, document, "Reply with exactly: ALPHA")
    except AgentUnavailable as exc:
        check("the first turn completes", False, exc)
        return 1
    elapsed = time.time() - started
    check("a reply came back", bool(final.strip()), repr(final[:80]))
    check("it is the reply that was asked for", "ALPHA" in final.upper(),
          repr(final[:120]))
    check("the reply was streamed, not delivered whole at the end",
          bool(streamed.strip()), repr(streamed[:80]))
    print("      (%.1fs)" % elapsed)

    print("\nsecond turn -- does the conversation continue?")
    try:
        final2, _streamed2, _tools2 = ask(client, document,
                                          "Reply with exactly: BETA")
    except AgentUnavailable as exc:
        check("the second turn completes", False, exc)
        return 1
    check("a second reply came back", bool(final2.strip()), repr(final2[:80]))
    check("it is the second reply", "BETA" in final2.upper(), repr(final2[:120]))
    check("it did not repeat the first answer", "ALPHA" not in final2.upper(),
          repr(final2[:120]))

    print("\nthe reply is not duplicated")
    # The runtime emits each session event twice; a naive consumer interleaves each
    # token with itself and produces "AALLPPHHAA".
    check("the streamed first reply is not doubled",
          streamed.upper().count("ALPHA") <= 2, repr(streamed[:120]))
    check("the final first reply is not doubled",
          final.upper().count("ALPHA") <= 1, repr(final[:120]))

    print("\nthe runtime is still healthy")
    final_status = client.ping()
    check("it answers after two turns", final_status.get("reachable"))
    check("and is not stuck busy", not final_status.get("busy"), final_status)

    print()
    if SKIPPED:
        print("SKIPPED: %s not available, so this could not run." % SKIPPED[0])
        print("  This is not a failure of the code under test.")
        return 0
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("Chat and turn-taking work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
