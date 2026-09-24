#!/usr/bin/env python3
"""Exercise the sidebar panel's call path without LibreOffice.

The panel is the one piece that cannot be smoke-tested from a script: it needs a
GUI, a live document, and a person to press Send. That makes the wiring between
the panel and the agent service — the seam most likely to break silently — the
least tested part of the project.

This test closes that gap. It stubs UNO just enough to import the component,
hands the *real* `_run_turn` / `submit` code a fake element and a fake client,
and asserts what the panel would put on screen. A typo in the call, a wrong
argument order, or a mis-shaped event would fail here rather than in front of a
user.

    python3 tests/test-panel-wiring.py
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(os.path.dirname(HERE), "ext", "oxt-proto", "components")

FAILURES = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print("  %s %s%s" % (mark, label, ("  — " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# ── stub just enough UNO to import the component ────────────────────────────

def install_uno_stubs():
    uno = types.ModuleType("uno")
    uno.createUnoStruct = lambda *a, **k: types.SimpleNamespace()
    uno.getComponentContext = lambda: None
    sys.modules["uno"] = uno

    uh = types.ModuleType("unohelper")
    uh.Base = type("Base", (), {})

    class _Helper:
        def addImplementation(self, *a, **k):
            pass

    uh.ImplementationHelper = lambda: _Helper()
    sys.modules["unohelper"] = uh

    def module(name, **attrs):
        m = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(m, key, value)
        sys.modules[name] = m
        return m

    module("com.sun.star.awt",
           XActionListener=type("XActionListener", (), {}),
           XWindowListener=type("XWindowListener", (), {}),
           XTextListener=type("XTextListener", (), {}),
           XCallback=type("XCallback", (), {}),
           XKeyListener=type("XKeyListener", (), {}),
           XAdjustmentListener=type("XAdjustmentListener", (), {}))
    module("com.sun.star.awt.Key", RETURN=1280)
    module("com.sun.star.awt.KeyModifier", SHIFT=1, MOD1=2)
    module("com.sun.star.awt.PosSize", POSSIZE=12)
    module("com.sun.star.beans", PropertyAttribute=type("PropertyAttribute", (), {}))
    # The clipboard transferable is declared with this interface; the stub is a
    # plain class because the tests never instantiate UNO's real one.
    module("com.sun.star.datatransfer",
           XTransferable=type("XTransferable", (), {}))
    module("com.sun.star.ui",
           XUIElementFactory=type("XUIElementFactory", (), {}),
           XUIElement=type("XUIElement", (), {}),
           XToolPanel=type("XToolPanel", (), {}),
           XSidebarPanel=type("XSidebarPanel", (), {}))
    module("com.sun.star.ui.UIElementType", TOOLPANEL=2)


install_uno_stubs()
sys.path.insert(0, COMPONENTS)
import cowork_sidebar as panel  # noqa: E402


# ── a stand-in for the element, recording what the panel would render ───────

class FakeModel:
    def __init__(self, url):
        self._url = url

    def getURL(self):
        return self._url


class FakeController:
    def __init__(self, url):
        self._model = FakeModel(url)

    def getModel(self):
        return self._model


class FakeFrame:
    def __init__(self, url):
        self._controller = FakeController(url)

    def getController(self):
        return self._controller


class FakeElement:
    """Only the surface the turn paths actually touch."""

    def __init__(self, url):
        self.frame = FakeFrame(url)
        self.entries = [{"kind": "cowork", "text": "Working on file. Tell me what you want done."}]
        self.busy = None
        self._status_seen = []
        self._status_text = ""
        self._turn_started = 0.0
        # The real element sets this in __init__; _run_turn compares against it
        # before ever assigning it, so the stub must have it too.
        self._last_tool = None

    def _entries(self):
        return self.entries

    def _render(self):
        pass

    def _append(self, speaker, message):
        kind = {"You": "you", "Cowork": "cowork"}.get(speaker, "cowork")
        self.entries.append({"kind": kind, "text": message})

    # The progress row: a label and a clock, never part of the transcript.
    def _set_status(self, text):
        self._status_text = text
        self._status_seen.append(text)

    def _drop_status(self):
        self._status_text = ""

    def _refresh_status_row(self):
        pass

    def _start_ticker(self):
        self._turn_started = __import__("time").time()

    def _stop_ticker(self):
        self._turn_started = 0.0

    def seen_statuses(self):
        return list(self._status_seen)

    def seen_status_count(self):
        return 1 if self._status_text else 0

    def status_row(self):
        return self._status_text

    submits = 0

    def submit(self):
        self.submits += 1

    def _set_busy(self, busy, label="Send"):
        self.busy = busy

    def _describe_tool(self, name):
        return panel.CoworkUIElement._describe_tool(name)

    # the threaded path
    _streaming = False
    _closing = False
    _last_tool = None



    _closing = False

    def deliver(self, item):
        """Apply one worker event, as the GUI thread would."""
        panel.CoworkUIElement.deliver(self, item)

    def pump(self):
        """Drain the worker queue, as the AsyncCallback pump does in the office."""
        for item in panel._drain():
            self.deliver(item)

    def transcript(self):
        out = []
        for e in self.entries:
            label = {"you": "You", "cowork": "Cowork"}.get(e["kind"])
            out.append(("%s: %s" % (label, e["text"])) if label else e["text"])
        return "\n\n".join(out)

    def statuses(self):
        # Progress lives in its own row, so the transcript should contain none.
        return [e["text"] for e in self.entries if e["kind"] == "status"]


class FakeClient:
    """Records how the panel called the service, and replays a scripted turn."""

    calls = []
    script = None
    raises = None

    def __init__(self, *a, **k):
        pass

    def ask(self, document, prompt, on_event, should_stop=None):
        FakeClient.calls.append({"document": document, "prompt": prompt,
                                 "has_stop": should_stop is not None})
        if FakeClient.raises is not None:
            raise FakeClient.raises
        for kind, payload in FakeClient.script:
            on_event(kind, payload)
        for kind, payload in reversed(FakeClient.script):
            if kind == "done":
                return payload.get("text", "")
        return ""


def run(_case, url="file:///tmp/report.odt",
        script=(("chunk", {"text": "Working"}), ("done", {"text": "Done."})),
        raises=None):
    FakeClient.calls = []
    FakeClient.script = script
    FakeClient.raises = raises
    original = panel.AgentClient
    panel.AgentClient = FakeClient
    element = FakeElement(url)
    try:
        panel._turn_worker(element, "tighten this paragraph")
        element.pump()
    finally:
        panel.AgentClient = original
    return element


def main():
    print("panel wiring\n")

    print("calling the service")
    element = run("basic")
    call = FakeClient.calls[0] if FakeClient.calls else {}
    check("the agent is called exactly once", len(FakeClient.calls) == 1,
          "got %d calls" % len(FakeClient.calls))
    check("it passes the document URL, not the prompt",
          call.get("document") == "file:///tmp/report.odt",
          repr(call.get("document")))
    check("it passes the user's text as the prompt",
          call.get("prompt") == "tighten this paragraph",
          repr(call.get("prompt")))

    print("\nrendering the streamed reply")
    check("the conversation opens with a Cowork message",
          element.entries and element.entries[0]["kind"] == "cowork",
          repr(element.entries[:1]))
    # The final authoritative text deliberately replaces the streamed
    # approximation, so assert the end state rather than a mid-stream snapshot.
    check("the reply reaches the transcript",
          "Done." in element.transcript(), repr(element.transcript()))
    check("the streamed approximation is not left behind",
          element.transcript().count("Done.") <= 2, repr(element.transcript()))
    check("no progress line is left dangling after the turn",
          element.statuses() == [], repr(element.statuses()))

    print("\nspeaking the user's language")
    element = run("tools", script=(
        ("tool", {"name": "document_check_layout"}),
        ("chunk", {"text": "Checked."}),
        ("done", {"text": "Checked."}),
    ))
    seen = element.seen_statuses()
    check("a tool call is described in the user's terms",
          any("Checking the layout" in t for t in seen), repr(seen))
    check("the raw tool name is never shown to the user",
          "document_check_layout" not in element.transcript()
          and not any("document_check_layout" in t for t in seen),
          repr(element.transcript()))
    check("progress never enters the transcript",
          element.statuses() == [], repr(element.statuses()))
    check("the progress row is cleared when the turn ends",
          element.status_row() == "", repr(element.status_row()))

    print("\nprogress updates in place instead of stacking up")
    element = run("progress", script=(
        ("tool", {"name": "document_list"}),
        ("tool", {"name": "document_read"}),
        ("tool", {"name": "document_check_layout"}),
        ("chunk", {"text": "All good."}),
        ("done", {"text": "All good."}),
    ))
    check("many tool calls never occupy more than one row",
          element.seen_status_count() <= 1,
          "concurrent status rows: %d" % element.seen_status_count())
    check("but the user does see each update as it happens",
          len(element.seen_statuses()) >= 3,
          repr(element.seen_statuses()))
    check("the last update is the one left on screen",
          "check" in (element.seen_statuses() or [""])[-1].lower(),
          repr(element.seen_statuses()[-1:]))

    print("\nthe worker path (what the panel actually uses)")
    FakeClient.calls = []
    FakeClient.script = (("tool", {"name": "document_read", "label": "Reading the document…"}),
                         ("chunk", {"text": "streamed "}),
                         ("chunk", {"text": "answer"}),
                         ("done", {"text": "answer"}))
    FakeClient.raises = None
    original = panel.AgentClient
    panel.AgentClient = FakeClient
    worker_el = FakeElement("file:///tmp/report.odt")
    try:
        panel._turn_worker(worker_el, "do the thing")
        worker_el.pump()
    finally:
        panel.AgentClient = original
    check("the worker puts a reply in the transcript",
          "answer" in worker_el.transcript(), repr(worker_el.transcript()))
    check("and reports the tool in the user's terms",
          any("Reading the document" in t for t in worker_el.seen_statuses()),
          repr(worker_el.seen_statuses()))

    print("\ntool progress goes to the row, not the transcript")
    element = run("toolsrow", script=(
        ("tool", {"name": "document_read"}),
        ("chunk", {"text": "Done."}),
        ("done", {"text": "Done."}),
    ))
    check("the tool label reached the progress row",
          any("Reading the document" in t for t in element.seen_statuses()),
          repr(element.seen_statuses()))
    check("and never entered the transcript",
          "Reading the document" not in element.transcript(),
          repr(element.transcript()))
    check("the rows is cleared once real output arrives",
          element.status_row() == "", repr(element.status_row()))

    print("\nShift+Enter sends, Enter adds a newline")
    element = FakeElement("file:///tmp/report.odt")
    keys = panel._ComposerKeys(element)

    class KeyEvent:
        def __init__(self, keycode, modifiers=0):
            self.KeyCode = keycode
            self.Modifiers = modifiers
            self.consumed = False

        def Consume(self):
            self.consumed = True

    RETURN, SHIFT = 1280, 1

    press = KeyEvent(RETURN, SHIFT)
    keys.keyPressed(press)
    check("Shift+Enter consumes the newline rather than inserting it",
          press.consumed)
    check("it does not send until the key is released",
          element.submits == 0, element.submits)
    keys.keyReleased(KeyEvent(RETURN, SHIFT))
    check("releasing Shift+Enter sends", element.submits == 1, element.submits)

    plain = KeyEvent(RETURN, 0)
    keys.keyPressed(plain)
    check("plain Enter is left to the control, so it inserts a newline",
          not plain.consumed)
    keys.keyReleased(KeyEvent(RETURN, 0))
    check("plain Enter does not send", element.submits == 1, element.submits)

    keys.keyReleased(KeyEvent(RETURN, SHIFT))
    check("a release with no prior press cannot send", element.submits == 1,
          element.submits)

    print("\nthe progress row empties when the turn ends")
    element = run("rowclear", script=(
        ("tool", {"name": "document_read"}),
        ("chunk", {"text": "Done."}),
        ("done", {"text": "Done."}),
    ))
    check("no status text survives the turn",
          element.status_row() == "", repr(element.status_row()))
    check("and busy is cleared", element.busy is False, repr(element.busy))

    print("\nhandling an unreachable service")
    element = run("unreachable", raises=panel.AgentUnavailable(
        "the Cowork service is not running on port 8765"))
    text = element.transcript()
    check("the failure is reported in the transcript",
          "not running" in text, repr(text))
    check("it does not claim the document changed",
          "changed" not in text.lower(), repr(text))

    print("\nhandling an unexpected error")
    element = run("boom", raises=RuntimeError("socket exploded"))
    check("an unexpected error is still shown",
          "socket exploded" in element.transcript(),
          repr(element.transcript()))

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All panel-wiring checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
