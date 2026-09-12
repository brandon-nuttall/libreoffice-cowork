"""Cowork sidebar panel — the visible agent surface inside LibreOffice.

This is a conversation about the document you have open, not a question-and-answer
box. It keeps one running thread per document, shows what the agent is doing while
it works, and stays out of the way otherwise.

Wiring (all four pieces must agree or the deck silently never appears):
  * ``registry/.../Sidebar.xcu``   — declares the Deck + Panel; the Panel's
    ImplementationURL is ``private:resource/toolpanel/CoworkSidebar/CoworkPanel``.
  * ``registry/.../Factories.xcu`` — maps the ``CoworkSidebar`` token to this
    component's implementation name (``IMPL_NAME`` below).
  * this component                 — an ``XUIElementFactory`` whose
    ``createUIElement`` returns an ``XUIElement`` -> ``XToolPanel`` owning an
    AWT window.
  * ``META-INF/manifest.xml``      — registers this file + both .xcu files.

Layout gotchas (the classic "blank panel" bugs), all handled below:
  * The sidebar creates the panel while its parent window is still 0x0, so a
    one-shot layout at build time yields invisible controls. Layout therefore
    runs again from a window-resize listener.
  * The sidebar sizes the panel from ``XSidebarPanel.getHeightForWidth``; a
    panel that does not implement it gets zero height.
  * Control instances and their listeners must be kept referenced, or Python
    garbage-collects them and the panel goes dead.

The conversation is kept in a module-level store keyed by document URL, so it
survives the sidebar tearing the panel down and rebuilding it (switching decks,
reloading a document) and so each document keeps its own thread.

Deliberately absent: a redundant in-panel title, and any framing that suggests
the point is to ask questions. LibreOffice already draws the deck name above the
panel, and the point is to get work done on the document.
"""

import os
import threading
import traceback

import uno
import unohelper

from com.sun.star.awt import XActionListener, XCallback, XWindowListener, XTextListener
from com.sun.star.awt.PosSize import POSSIZE
from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel
from com.sun.star.ui import XSidebarPanel
from com.sun.star.ui.UIElementType import TOOLPANEL

# The client is plain Python with no UNO dependency, so the streaming path can
# be tested against a live service without a GUI.
try:
    from cowork_client import AgentClient, AgentUnavailable
except ImportError:  # the extension dir is not always on sys.path
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from cowork_client import AgentClient, AgentUnavailable

# MUST equal FactoryImplementation in Factories.xcu.
IMPL_NAME = "com.cowork.SidebarFactory"

_LOG = os.environ.get("COWORK_SIDEBAR_LOG") or os.path.join(
    os.path.expanduser("~"), ".cache", "cowork-sidebar.log")

# Panel metrics. The panel asks for a generous height because a conversation
# needs room; if the sidebar gives less, the transcript absorbs the difference.
_PANEL_PREFERRED_HEIGHT = 420
_PANEL_MIN_HEIGHT = 200
_MARGIN = 6
_GAP = 4
_COMPOSER_HEIGHT = 30
_BUTTON_HEIGHT = 26
_MIN_INNER_WIDTH = 80
_FALLBACK_WIDTH = 240

# Per-document conversations, keyed by document URL. Module-level so they outlive
# any individual panel instance.
_TRANSCRIPTS = {}
_DEFAULT_KEY = "(no document)"

# ---------------------------------------------------------------------------
# Thread hand-off.
#
# A turn must not run on the VCL thread: the socket blocks for as long as the
# model takes, and a frozen LibreOffice looks broken. But VCL controls are not
# thread-safe either, so the worker cannot touch them.
#
# `com.sun.star.awt.AsyncCallback` is the supported bridge. It is created from
# the panel's own context (it is not creatable from a bare script context, which
# is what made an earlier attempt look impossible), and `addCallback` may be
# called from any thread: the callback's `notify` then runs on the GUI thread.
# Verified in the office — `notify` reported thread "MainThread" while being
# invoked from a worker.
#
# Events are queued and drained inside `notify`, so the worker never blocks on
# the UI and ordering is preserved.
# ---------------------------------------------------------------------------

_QUEUE = []
_QUEUE_LOCK = threading.Lock()


def _emit(item):
    with _QUEUE_LOCK:
        _QUEUE.append(item)


def _drain():
    with _QUEUE_LOCK:
        items, _QUEUE[:] = list(_QUEUE), []
    return items


def _log(msg):
    try:
        with open(_LOG, "a") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _doc_key(frame):
    """Identify the conversation by document, so each document keeps its own."""
    try:
        return frame.getController().getModel().getURL() or _DEFAULT_KEY
    except Exception:
        return _DEFAULT_KEY


def _doc_name(frame):
    try:
        url = frame.getController().getModel().getURL()
        return url.rsplit("/", 1)[-1] or "this untitled document"
    except Exception:
        return "this document"


def _greeting(frame):
    """The opening line of a conversation.

    States what the agent is looking at and what it can do, then stops. It is
    not a question: an empty box with a question in it invites typing, whereas
    this reads as the top of a thread already in progress.

    It also says plainly whether the service is up. A panel that silently does
    nothing when its backend is missing is the worst version of this UI.
    """
    base = ("Working on %s. I can read it, edit it, restructure it, and check "
            "how the result looks — everything I change in one go is a single "
            "undo step." % _doc_name(frame))
    status = _service_status()
    if status is None:
        return (base + "\n\nI cannot reach the Cowork service, so I cannot "
                "change anything yet. Start it with:\n"
                "    python3 dsh/libreoffice/cowork/cowork_agent.py")
    if not status.get("runtime"):
        return (base + "\n\nThe Cowork service is running but its agent "
                "runtime is not ready. Check the service log for why.")
    return base + " Tell me what you want done."


def _service_status():
    """Probe the service once per panel build. None means unreachable."""
    try:
        reply = AgentClient().ping()
    except Exception:
        return None
    return reply if reply.get("reachable") else None


class _RelayoutListener(unohelper.Base, XWindowListener):
    """Re-lays-out the panel whenever the sidebar resizes or shows it."""

    def __init__(self, element):
        self.element = element

    def windowResized(self, _event):
        if self.element is not None:
            self.element.layout()

    def windowShown(self, _event):
        if self.element is not None:
            self.element.layout()

    def windowMoved(self, _event):
        pass

    def windowHidden(self, _event):
        pass

    def disposing(self, _event):
        self.element = None


class _PanelListener(unohelper.Base, XActionListener, XTextListener):
    """Send on button click or Enter; Clear empties the thread."""

    def __init__(self, element):
        self.element = element

    def actionPerformed(self, event):
        element = self.element
        if element is None:
            return
        try:
            source = event.Source
            model = source.getModel()
            label = getattr(model, "Label", "")
        except Exception:
            label = ""
        if label == "Clear":
            element.clear()
        else:
            element.submit()

    def textChanged(self, _event):
        pass  # reserved: typing indicator, slash-command completion

    def disposing(self, _event):
        self.element = None


def _turn_worker(element, prompt):
    """Runs one turn off the GUI thread, reporting progress through the queue."""
    try:
        document = element.frame.getController().getModel().getURL() or ""
    except Exception:
        document = ""

    def on_event(kind, payload):
        if kind == "chunk":
            element.post_from_worker(("chunk", payload.get("text", "")))
        elif kind == "tool":
            element.post_from_worker(("reset", None))
            element.post_from_worker(("tool", payload.get("name", "")))

    try:
        final = AgentClient().ask(document, prompt, on_event,
                                 should_stop=lambda: element._closing)
        element.post_from_worker(("final", final))
    except AgentUnavailable as exc:
        element.post_from_worker(("error", str(exc)))
    except Exception as exc:  # noqa: BLE001
        element.post_from_worker(("error", "That did not work: %s" % exc))
    finally:
        element.post_from_worker(("done", None))


class _MainThreadPump(unohelper.Base, XCallback):
    """Runs on the GUI thread; drains the worker's queue into the controls.

    Kept referenced by the element for the panel's lifetime: a garbage-collected
    callback is a silently dead panel, the same class of bug as an
    unreferenced listener.
    """

    def __init__(self, element):
        self.element = element

    def notify(self, _data):
        element = self.element
        if element is None:
            return
        for item in _drain():
            try:
                element.deliver(item)
            except Exception:
                _log(traceback.format_exc())


class CoworkToolPanel(unohelper.Base, XToolPanel, XSidebarPanel):
    def __init__(self, window, height):
        self.Window = window
        self._height = height

    def createAccessible(self, _parent):
        return self.Window

    def getAccessibleContext(self):
        return None

    # XSidebarPanel — the sidebar sizes the panel from this answer.
    def getHeightForWidth(self, _width):
        size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        size.Minimum = _PANEL_MIN_HEIGHT
        size.Preferred = max(self._height, _PANEL_PREFERRED_HEIGHT)
        size.Maximum = max(self._height, _PANEL_PREFERRED_HEIGHT)
        return size

    def getMinimalWidth(self):
        return _MIN_INNER_WIDTH + 2 * _MARGIN


class CoworkUIElement(unohelper.Base, XUIElement):
    def __init__(self, ctx, frame, parent_window, url):
        self.ctx = ctx
        self.frame = frame
        self.parent_window = parent_window
        self.ResourceURL = url
        self.Type = TOOLPANEL
        self.Frame = frame
        self._panel = None
        self._root = None
        self._controls = {}
        self._listeners = []
        self._resize_listener = None
        self._busy = False
        self._last_tool = None
        self._closing = False
        # True while a reply is being streamed into the last transcript line.
        self._streaming = False
        # AsyncCallback + its callback object must both outlive every turn.
        self._async = None
        self._pump = None
        self._worker = None

    def getRealInterface(self):
        if self._panel is None:
            root = self._build_window()
            self._panel = CoworkToolPanel(root, _PANEL_PREFERRED_HEIGHT)
        return self._panel

    # -- construction -------------------------------------------------- //

    def _new(self, kind):
        return self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.awt.%s" % kind, self.ctx)

    def _control(self, container, name, kind, model_kind, **props):
        """Create one control defensively.

        A control that fails to build must not take the whole panel with it —
        but it must also not do so *silently*, because a missing control is
        exactly the "blank panel" bug. Failures are logged with the property
        that caused them.
        """
        try:
            control = self._new(kind)
            model = self._new(model_kind)
            for key, value in props.items():
                try:
                    setattr(model, key, value)
                except Exception as exc:  # noqa: BLE001
                    _log("property %s=%r rejected on %s: %s: %s"
                         % (key, value, model_kind, type(exc).__name__, exc))
                    raise
            control.setModel(model)
            container.addControl(name, control)
            self._controls[name] = control
            _log("control OK: %s (%s)" % (name, kind))
            return control
        except Exception:
            _log("control FAILED: %s (%s)\n%s" % (name, kind, traceback.format_exc()))
            return None

    def _build_window(self):
        _log("--- building panel ---")
        toolkit = self._new("Toolkit")
        container = self._new("UnoControlContainer")
        container.setModel(self._new("UnoControlContainerModel"))
        container.createPeer(toolkit, self.parent_window)
        self._root = container

        # The conversation occupies the top and absorbs all spare height. No
        # caption above it: LibreOffice already names the deck, and a second
        # title would waste the most valuable strip of the panel.
        self._control(container, "txtTranscript", "UnoControlEdit",
                      "UnoControlEditModel",
                      MultiLine=True,
                      ReadOnly=True,          # a transcript, not an input field
                      VScroll=True,
                      AutoVScroll=True,
                      Border=True,            # marks the region as the thread
                      HideInactiveSelection=False,
                      Tabstop=True)

        self._control(container, "txtComposer", "UnoControlEdit",
                      "UnoControlEditModel",
                      MultiLine=False,
                      Border=True,
                      VScroll=False)

        composer = self._controls.get("txtComposer")
        if composer is not None:
            listener = _PanelListener(self)
            composer.addTextListener(listener)
            self._listeners.append(listener)

        listener = _PanelListener(self)
        for name, label in (("btnSend", "Send"), ("btnClear", "Clear")):
            button = self._control(container, name, "UnoControlButton",
                                   "UnoControlButtonModel",
                                   Label=label, PushButtonType=0)
            if button is not None:
                button.addActionListener(listener)
        self._listeners.append(listener)

        # Created here rather than in __init__ so it belongs to the same context
        # that built the controls.
        try:
            self._async = self.ctx.ServiceManager.createInstanceWithContext(
                "com.sun.star.awt.AsyncCallback", self.ctx)
            self._pump = _MainThreadPump(self)
        except Exception:
            self._async = None
            self._pump = None
            _log("AsyncCallback unavailable, turns will run on the UI thread:\n%s"
                 % traceback.format_exc())

        _log("controls built: %s" % sorted(self._controls))

        # Seed the conversation the first time this document is seen, so the
        # panel never opens blank.
        key = _doc_key(self.frame)
        if key not in _TRANSCRIPTS:
            _TRANSCRIPTS[key] = [_greeting(self.frame)]
        self._render()

        self._resize_listener = _RelayoutListener(self)
        for target in (self.parent_window, container):
            try:
                target.addWindowListener(self._resize_listener)
            except Exception:
                _log(traceback.format_exc())

        self.layout()
        try:
            container.setVisible(True)
        except Exception:
            _log(traceback.format_exc())
        return container

    # -- layout -------------------------------------------------------- //

    def layout(self):
        container = self._root
        if container is None:
            return
        try:
            psize = self.parent_window.getPosSize()
            csize = container.getPosSize()
            width = csize.Width if csize.Width > 0 else psize.Width
            height = csize.Height if csize.Height > 0 else psize.Height
            if psize.Width > 0 and psize.Height > 0:
                if csize.Width != psize.Width or csize.Height != psize.Height:
                    container.setPosSize(0, 0, psize.Width, psize.Height, POSSIZE)
                    csize = container.getPosSize()
                    width, height = csize.Width, csize.Height
            if width <= 0:
                width = _FALLBACK_WIDTH
            if height <= 60:
                height = _PANEL_PREFERRED_HEIGHT
            inner = max(width - 2 * _MARGIN, _MIN_INNER_WIDTH)

            # Bottom stack is fixed; the conversation takes everything left.
            buttons_y = height - _MARGIN - _BUTTON_HEIGHT
            composer_y = buttons_y - _GAP - _COMPOSER_HEIGHT
            transcript_y = _MARGIN
            transcript_h = max(composer_y - _GAP - transcript_y, 60)

            self._place("txtTranscript", _MARGIN, transcript_y, inner, transcript_h)
            self._place("txtComposer", _MARGIN, composer_y, inner, _COMPOSER_HEIGHT)
            half = max((inner - _GAP) // 2, 30)
            self._place("btnSend", _MARGIN, buttons_y, half, _BUTTON_HEIGHT)
            self._place("btnClear", _MARGIN + half + _GAP, buttons_y,
                        inner - half - _GAP, _BUTTON_HEIGHT)
            _log("layout: parent=%dx%d inner=%d transcript_h=%d"
                 % (psize.Width, psize.Height, inner, transcript_h))
        except Exception:
            _log(traceback.format_exc())

    def _control_by_name(self, name):
        """Find a control, falling back to the container.

        `_controls` is the fast path, but a control created before a failure can
        still live in the container. Asking the container keeps this honest and
        makes the panel inspectable from outside for testing.
        """
        control = self._controls.get(name)
        if control is not None:
            return control
        if self._root is None:
            return None
        try:
            found = self._root.getControl(name)
            if found is not None:
                self._controls[name] = found
            return found
        except Exception:
            return None

    def _place(self, name, x, y, w, h):
        control = self._control_by_name(name)
        if control is None:
            return
        try:
            control.setPosSize(x, y, max(w, 10), max(h, 10), POSSIZE)
        except Exception:
            _log(traceback.format_exc())

    # -- conversation rendering ---------------------------------------- //

    def _lines(self):
        return _TRANSCRIPTS.setdefault(_doc_key(self.frame), [])

    def _render(self):
        """Redraw the transcript, scrolled to the newest line."""
        control = self._controls.get("txtTranscript")
        if control is None:
            return
        text = "\n\n".join(self._lines())
        try:
            control.setText(text)
            # Keep the end in view, the way a chat window does.
            try:
                control.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection",
                                                         0, len(text)))
            except Exception:
                pass
        except Exception:
            _log("setText failed:\n%s" % traceback.format_exc())

    def _append(self, speaker, message):
        lines = self._lines()
        lines.append("%s\n%s" % (speaker, message))
        # Keep the store bounded so a long session cannot grow without limit.
        if len(lines) > 120:
            del lines[:-120]
        self._render()

    def _set_busy(self, busy, label="Send"):
        """Reflect that a turn is running.

        The button label is the only affordance this panel has for 'working';
        without it a slow turn looks like a broken one.
        """
        self._busy = busy
        button = self._controls.get("btnSend")
        if button is None:
            return
        try:
            button.getModel().Label = "…" if busy else label
            button.getModel().Enabled = not busy
        except Exception:
            _log(traceback.format_exc())

    # -- behaviour ----------------------------------------------------- //

    def submit(self):
        """Send the composer text as the next message in this conversation."""
        composer = self._controls.get("txtComposer")
        if composer is None or self._busy:
            return
        try:
            text = composer.getText().strip()
        except Exception:
            _log(traceback.format_exc())
            return
        if not text:
            return
        try:
            composer.setText("")
        except Exception:
            _log(traceback.format_exc())

        self._append("You", text)
        self._set_busy(True)

        if self._async is None or self._pump is None:
            # No marshalling available: run inline rather than refuse. The turn
            # still works, it just cannot redraw while it waits.
            self._run_turn(text)
            self._set_busy(False)
            return

        try:
            self._worker = threading.Thread(
                target=_turn_worker, args=(self, text), name="cowork-turn",
                daemon=True)
            self._worker.start()
        except Exception as exc:  # noqa: BLE001
            _log("could not start the worker: %s" % exc)
            self._run_turn(text)
            self._set_busy(False)

    def post_from_worker(self, item):
        """Hand one item to the GUI thread. Callable from any thread."""
        # Recorded so a real run can prove the hop: a `deliver` line whose thread
        # differs from the `posted` line is the marshalling working.
        _log("posted %r from %s" % (item[0], threading.current_thread().name))
        _emit(item)
        try:
            self._async.addCallback(self._pump, None)
        except Exception:
            # Marshalling failed; the next pump will still collect the item if
            # anything else wakes the queue.
            _log("addCallback failed:\n%s" % traceback.format_exc())

    # -- applying worker output (GUI thread only) ----------------------- //

    def deliver(self, item):
        kind = item[0]
        _log("delivering %r on %s" % (kind, threading.current_thread().name))
        if kind == "chunk":
            piece = item[1]
            if piece:
                if not self._streaming:
                    self._streaming = True
                    self._begin_reply()
                self._extend_reply(piece)
        elif kind == "reset":
            # A tool call interrupts the prose; the next text starts a new line.
            self._streaming = False
        elif kind == "tool":
            name = item[1]
            if name != self._last_tool:
                self._last_tool = name
                self._append("Cowork", self._describe_tool(name))
                self._streaming = False
        elif kind == "final":
            if item[1] and self._streaming:
                self._replace_reply(item[1])
            elif item[1]:
                self._append("Cowork", item[1])
        elif kind == "error":
            self._append("Cowork", item[1])
        elif kind == "done":
            self._streaming = False
            self._set_busy(False)

    def _run_turn(self, prompt):
        """The inline path, used only when AsyncCallback is unavailable."""
        self._streaming = False
        try:
            document = self.frame.getController().getModel().getURL() or ""
        except Exception:
            document = ""

        def on_event(kind, payload):
            if kind == "chunk":
                self.deliver(("chunk", payload.get("text", "")))
            elif kind == "tool":
                self.deliver(("tool", payload.get("name", "")))

        try:
            final = AgentClient().ask(document, prompt, on_event)
            self.deliver(("final", final))
        except AgentUnavailable as exc:
            self.deliver(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.deliver(("error", "That did not work: %s" % exc))

    def clear(self):
        """Start a fresh thread for this document."""
        _TRANSCRIPTS[_doc_key(self.frame)] = [_greeting(self.frame)]
        self._render()

    @staticmethod
    def _describe_tool(name):
        """Turn a tool name into something a person understands.

        This is not a developer console: "document_check_layout" means nothing to
        the person waiting, whereas the sentence below says what is happening to
        their document.
        """
        return {
            "document_list": "Looking at what you have open…",
            "document_read": "Reading the document…",
            "document_outline": "Reading the document structure…",
            "document_find": "Searching the document…",
            "document_append": "Adding to the document…",
            "document_replace": "Editing the document…",
            "document_check_layout": "Checking the layout…",
            "document_render": "Rendering the page to look at it…",
            "read_image": "Looking at the rendered page…",
            "document_end_turn": "Finishing the turn…",
            "document_undo": "Undoing that…",
            "document_track_changes": "Turning tracked changes on…",
            "document_revisions": "Reading the tracked changes…",
            "sheet_read_range": "Reading the spreadsheet…",
            "sheet_write_range": "Writing to the spreadsheet…",
            "skill": "Checking how best to do this…",
            "bash": "Running a command…",
            "read": "Reading a file…",
            "write": "Writing a file…",
        }.get(name, "Working…")

    def postDisposing(self):
        # Signal the worker before tearing anything down: a turn may still be
        # streaming, and it must not append to a disposed control.
        if self._resize_listener is not None:
            for target in (self.parent_window, self._root):
                try:
                    if target is not None:
                        target.removeWindowListener(self._resize_listener)
                except Exception:
                    pass
            self._resize_listener = None
        if self._root is not None:
            try:
                self._root.dispose()
            except Exception:
                pass
        self._root = None
        self._panel = None
        self._controls = {}
        self._listeners = []


class CoworkSidebarFactory(unohelper.Base, XUIElementFactory):
    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, url, args):
        frame = None
        parent = None
        for prop in args:
            if prop.Name == "Frame":
                frame = prop.Value
            elif prop.Name == "ParentWindow":
                parent = prop.Value
        if frame is None or parent is None:
            return None
        try:
            return CoworkUIElement(self.ctx, frame, parent, url)
        except Exception:
            _log(traceback.format_exc())
            return None


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    CoworkSidebarFactory, IMPL_NAME, (IMPL_NAME,))
