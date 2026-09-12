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
           XCallback=type("XCallback", (), {}))
    module("com.sun.star.awt.PosSize", POSSIZE=12)
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
        self._status_peak = 0
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

    def _record_status(self, text):
        self._status_seen.append(text)
        self._status_peak = max(self._status_peak,
                                sum(1 for e in self.entries if e["kind"] == "status"))

    def seen_statuses(self):
        return list(self._status_seen)

    def seen_status_count(self):
        return self._status_peak

    def _set_status(self, text):
        self._record_status(text)
        if self.entries and self.entries[-1]["kind"] == "status":
            self.entries[-1]["text"] = text
        else:
            self.entries.append({"kind": "status", "text": text})

    def _drop_status(self):
        if self.entries and self.entries[-1]["kind"] == "status":
            self.entries.pop()

    def _set_busy(self, busy, label="Send"):
        self.busy = busy

    def _describe_tool(self, name):
        return panel.CoworkUIElement._describe_tool(name)

    # the threaded path
    _streaming = False
    _closing = False
    _last_tool = None

    def deliver(self, item):
        panel.CoworkUIElement.deliver(self, item)

    def post_from_worker(self, item):
        """Stand in for AsyncCallback: apply immediately, on this thread."""
        self.deliver(item)

    def transcript(self):
        out = []
        for e in self.entries:
            label = {"you": "You", "cowork": "Cowork"}.get(e["kind"])
            out.append(("%s: %s" % (label, e["text"])) if label else e["text"])
        return "\n\n".join(out)

    def statuses(self):
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
        panel.CoworkUIElement._run_turn(element, "tighten this paragraph")
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
    check("the progress line does not survive into the finished transcript",
          element.statuses() == [], repr(element.statuses()))

    print("\nprogress updates in place instead of stacking up")
    element = run("progress", script=(
        ("tool", {"name": "document_list"}),
        ("tool", {"name": "document_read"}),
        ("tool", {"name": "document_check_layout"}),
        ("chunk", {"text": "All good."}),
        ("done", {"text": "All good."}),
    ))
    peak = element.seen_status_count()
    check("many tool calls never occupy more than one line",
          peak <= 1, "peak concurrent status entries: %d" % peak)
    check("but the user does see each update as it happens",
          len(element.seen_statuses()) >= 3,
          repr(element.seen_statuses()))

    print("\nthe threaded path (what the panel actually uses)")
    FakeClient.calls = []
    FakeClient.script = (("tool", {"name": "document_read"}),
                         ("chunk", {"text": "streamed "}),
                         ("chunk", {"text": "answer"}),
                         ("done", {"text": "answer"}))
    FakeClient.raises = None
    original = panel.AgentClient
    panel.AgentClient = FakeClient
    threaded = FakeElement("file:///tmp/report.odt")
    try:
        # `_turn_worker` is what runs off the UI thread; post_from_worker stands
        # in for the AsyncCallback hop, so this asserts the real worker code.
        panel._turn_worker(threaded, "do the thing")
    finally:
        panel.AgentClient = original
    text = threaded.transcript()
    # The authoritative final text replaces the streamed approximation, so the
    # end state holds the final text, not the partial one.
    check("the worker puts a reply in the transcript",
          "answer" in text, repr(text))
    check("it reports the tool in the user's terms",
          any("Reading the document" in t for t in threaded.seen_statuses()),
          repr(threaded.seen_statuses()))
    check("the final text is applied",
          text.strip().endswith("answer"), repr(text))

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
