#!/usr/bin/env python3
"""Dump the panel's layout as numbers, so layout work stops needing screenshots.

Iterating on this panel the slow way — rebuild, restart LibreOffice, capture,
look — costs about a minute per attempt and converges badly. Most layout
questions are not visual at all: is the row inside the viewport, is its height
bigger than its font, is it hidden, is the bubble colour set. Those are numbers,
and this prints them.

With the panel open and the viewport height known, a layout bug is usually
obvious from the numbers alone; a capture is then only needed to confirm taste,
not to find the fault.

    python3 tests/panel-layout.py                 # uses the sandbox office
    python3 tests/panel-layout.py --port 2098
"""

import sys

sys.path.insert(0, "/usr/lib/python3/dist-packages")

import uno  # noqa: E402
from com.sun.star.beans import PropertyValue  # noqa: E402


def connect(port):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)
    return resolver.resolve(
        "uno:socket,host=127.0.0.1,port=%d;urp;StarOffice.ComponentContext" % port)


def current_frame(smgr, ctx):
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    enumeration = desktop.Components.createEnumeration()
    while enumeration.hasMoreElements():
        component = enumeration.nextElement()
        try:
            if component.supportsService("com.sun.star.text.TextDocument") \
                    and component.getURL():
                return component.getCurrentController().getFrame()
        except Exception:
            continue
    raise SystemExit("no document open in the office")


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def main():
    port = 2098
    args = sys.argv[1:]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])

    ctx = connect(port)
    smgr = ctx.ServiceManager
    frame = current_frame(smgr, ctx)

    factory = smgr.createInstanceWithContext("com.cowork.SidebarFactory", ctx)
    element = factory.createUIElement(
        "private:resource/toolpanel/CoworkSidebar/CoworkPanel",
        (prop("Frame", frame), prop("ParentWindow", frame.getContainerWindow())))
    container = element.getRealInterface().createAccessible(None)

    transcript = container.getControl("pnlTranscript")
    size = transcript.getPosSize()
    print("pnlTranscript  x=%d y=%d w=%d h=%d" % (size.X, size.Y, size.Width, size.Height))
    print()

    print("%-5s %-6s %-5s %-5s %-5s %-6s %-4s %-9s %-9s %s"
          % ("row", "y", "h", "w", "vis", "size", "algn", "colour", "bg", "label"))
    for index in range(60):
        try:
            control = transcript.getControl("row%d" % index)
        except Exception:
            break
        if control is None:
            break
        pos = control.getPosSize()
        model = control.getModel()
        offscreen = "  <-- BELOW VIEW" if pos.Y > size.Height else ""
        tiny = ""
        if pos.Height < model.FontHeight * 3.5:
            tiny = "  <-- HEIGHT < TEXT"
        print("%-5d %-6d %-5d %-5d %-5s %-6s %-4s 0x%06x  0x%06x  %r%s%s"
              % (index, pos.Y, pos.Height, pos.Width,
                 "yes" if control.isVisible() else "no",
                 model.FontHeight, model.Align,
                 model.TextColor & 0xFFFFFF,
                 model.BackgroundColor & 0xFFFFFF,
                 model.Label[:34], offscreen, tiny))

    rows = sum(1 for i in range(60)
               if (transcript.getControl("row%d" % i) if True else None) is not None)
    print()
    print("last row bottom vs viewport: see y + h above; viewport height = %d"
          % size.Height)
    return 0


if __name__ == "__main__":
    sys.exit(main())
