#!/usr/bin/env python3
"""Boot-test the COWORK panel against a live office.

This test exists because a panel build bug shipped twice (an attribute read on
the wrong module; an uninitialised pool) after "all checks passed" — the unit
suite never builds the real panel. This one does: createUIElement must return a
usable element, the control set must include the current design's controls,
and nothing may throw.

Skips (exit 0) when no office is reachable, like the other live tests.

    python3 tests/test-panel-build.py [--accept <spec>] [--port <n>]
"""

import os
import sys
import time

sys.path.insert(0, "/usr/lib/python3/dist-packages")

import uno  # noqa: E402
from com.sun.star.beans import PropertyValue  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        FAILURES.append(label)


def connect(spec):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)
    return resolver.resolve(
        "uno:%s;urp;StarOffice.ComponentContext" % spec)


def main():
    spec = os.environ.get("COWORK_ACCEPT", "socket,host=127.0.0.1,port=2098")
    args = sys.argv[1:]
    if "--accept" in args:
        spec = args[args.index("--accept") + 1]

    try:
        ctx = connect(spec)
    except Exception:
        print("SKIPPED: no reachable office (%s)" % spec)
        print("  This is not a failure of the code under test.")
        return 0

    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = None
    enum = desktop.Components.createEnumeration()
    while enum.hasMoreElements():
        c = enum.nextElement()
        try:
            if c.supportsService("com.sun.star.text.TextDocument") and c.getURL():
                doc = c
                break
        except Exception:
            continue
    if doc is None:
        print("SKIPPED: office reachable but no document is open")
        return 0

    frame = doc.getCurrentController().getFrame()

    def pv(n, v):
        p = PropertyValue()
        p.Name = n
        p.Value = v
        return p

    print("panel build\n")
    factory = smgr.createInstanceWithContext("com.cowork.SidebarFactory", ctx)
    element = factory.createUIElement(
        "private:resource/toolpanel/CoworkSidebar/CoworkPanel",
        (pv("Frame", frame), pv("ParentWindow", frame.getContainerWindow())))
    check("createUIElement returns a live element", element is not None, element)
    if element is None:
        print("\nFAILED: the panel did not build; see the sidebar log")
        return 1

    try:
        container = element.getRealInterface().createAccessible(None)
    except Exception as exc:  # noqa: BLE001
        check("getRealInterface works", False, exc)
        return 1

    # Per-name lookup: getControls() on the accessible has returned empty on
    # live offices where getControl(name) resolves fine (the reverse of an
    # earlier bug -- enums and named lookup go through different paths).
    def built(name):
        try:
            return container.getControl(name) is not None
        except Exception:
            return False

    names = [n for n in ("btnCommands", "btnSend", "btnClear", "btnCopy",
                        "pnlTranscript", "txtComposer", "scrTranscript")
             if built(n)]
    print("      controls found: %s" % names)

    check("the command palette button is built", built("btnCommands"), names)
    check("the up-arrow send is built", built("btnSend"), names)
    check("the old Clear/Copy row is gone",
          not built("btnClear") and not built("btnCopy"), names)
    check("the transcript container is built", built("pnlTranscript"), names)
    check("the composer is built", built("txtComposer"), names)
    check("the scrollbar is built", built("scrTranscript"), names)

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("The panel builds and matches the current design.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
