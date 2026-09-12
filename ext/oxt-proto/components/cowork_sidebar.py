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

from com.sun.star.awt import (XActionListener, XCallback, XKeyListener,
                              XWindowListener, XTextListener)
from com.sun.star.awt.Key import RETURN as KEY_RETURN
from com.sun.star.awt.KeyModifier import SHIFT as MOD_SHIFT
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
_COMPOSER_HEIGHT = 46
_STATUS_HEIGHT = 18
# Show the elapsed clock only after this long, matching the DSH progress chrome:
# a count-up on a two-second call is noise, on a thirty-second one it is the
# difference between "working" and "hung".
_CLOCK_AFTER_MS = 15000
_BUTTON_HEIGHT = 26
# The width the panel needs to be legible, not the width it can survive. This is
# reported through XSidebarPanel.getMinimalWidth(), and the sidebar uses it to
# decide how wide to open. Declaring 80 let the sidebar open at its own 92 px
# minimum, which wrapped every line after two or three characters and made the
# panel unreadable (seen in the sandbox capture).
_MIN_INNER_WIDTH = 220
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


class _Ticker(unohelper.Base, XCallback):
    """Re-renders the progress row once a second, on the GUI thread."""

    def __init__(self, element):
        self.element = element

    def notify(self, _data):
        element = self.element
        if element is None:
            return
        try:
            element._refresh_status_row()
        except Exception:
            _log(traceback.format_exc())


class _ComposerKeys(unohelper.Base, XKeyListener):
    """Shift+Enter sends; Enter inserts a newline.

    A multiline composer needs both, and the toolkit's own behaviour only gives
    the newline. The split is deliberate:

      * Shift+Enter is intercepted in `keyPressed`, the event is consumed, and
        the send is issued in `keyReleased`.
      * Plain Enter is left entirely alone, so the control inserts the newline
        itself and wrap-and-continue behaves exactly as it does in any other
        multi-line field.

    Consuming rather than merely observing matters: if Shift+Enter were allowed
    through, the control would append a newline and then the send would fire,
    leaving the composer holding a stray blank line.

    Requires `XDispatchProvider`-style completeness of the interface: XKeyListener
    declares both `keyPressed` and `keyReleased`, and pyuno rejects the object if
    either is missing.
    """

    def __init__(self, element):
        self.element = element
        # Set when Shift+Enter was consumed, so the matching keyReleased sends
        # rather than being treated as an ordinary key-up.
        self._pending_send = False

    def keyPressed(self, event):
        element = self.element
        if element is None:
            return
        if event.KeyCode != KEY_RETURN:
            return
        modifiers = getattr(event, "Modifiers", 0) or 0
        if not (modifiers & MOD_SHIFT):
            return                      # plain Enter: let the control add a newline
        self._pending_send = True
        event.Consume()                 # stop the newline before it is inserted

    def keyReleased(self, event):
        element = self.element
        if element is None or not self._pending_send:
            return
        self._pending_send = False
        if event.KeyCode == KEY_RETURN:
            element.submit()

    def disposing(self, _event):
        self.element = None


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
        """Tell the sidebar how tall this panel can be.

        Maximum matters: a panel that caps it at its preferred height gets a
        fixed strip and leaves the rest of the deck empty, which is what made the
        conversation occupy the top third of a tall sidebar. The panel has no
        intrinsic height — it is a conversation, and a conversation wants whatever
        room there is — so the maximum is effectively unbounded and the preferred
        is only a hint for the first layout pass.
        """
        size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        size.Minimum = _PANEL_MIN_HEIGHT
        size.Preferred = max(self._height, _PANEL_PREFERRED_HEIGHT)
        size.Maximum = 32767
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
        # Progress row state.
        self._status_text = ""
        self._turn_started = 0.0
        self._ticker = None
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

        # MultiLine + no horizontal scroll: a single-line field scrolls the text
        # sideways as you type, so anything longer than the box width is
        # invisible. Wrapping keeps the whole message readable.
        # Progress row: an indeterminate bar (the toolkit animates it for us, so
        # there is visible motion with no timer at all), a label naming what is
        # happening, and a count-up clock.
        self._control(container, "prgStatus", "UnoControlProgressBar",
                      "UnoControlProgressBarModel",
                      ProgressValue=0, ProgressValueMin=0, ProgressValueMax=100)

        self._control(container, "lblStatus", "UnoControlFixedText",
                      "UnoControlFixedTextModel",
                      Label="", MultiLine=False, Align=0)

        self._control(container, "txtComposer", "UnoControlEdit",
                      "UnoControlEditModel",
                      MultiLine=True,
                      Border=True,
                      VScroll=True,
                      AutoVScroll=True,
                      HScroll=False,
                      AutoHScroll=False)

        composer = self._controls.get("txtComposer")
        if composer is not None:
            listener = _PanelListener(self)
            composer.addTextListener(listener)
            self._listeners.append(listener)
            key_listener = _ComposerKeys(self)
            try:
                composer.addKeyListener(key_listener)
                self._listeners.append(key_listener)
            except Exception:
                _log("could not attach the key listener; Shift+Enter will not "
                     "send:\n%s" % traceback.format_exc())

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
            _TRANSCRIPTS[key] = [{"kind": "cowork", "text": _greeting(self.frame)}]
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
            resized = False
            if psize.Width > 0 and psize.Height > 0:
                if csize.Width != psize.Width or csize.Height != psize.Height:
                    container.setPosSize(0, 0, psize.Width, psize.Height, POSSIZE)
                    csize = container.getPosSize()
                    width, height = csize.Width, csize.Height
                    resized = True
            if width <= 0:
                width = _FALLBACK_WIDTH
            if height <= 60:
                height = _PANEL_PREFERRED_HEIGHT
            inner = max(width - 2 * _MARGIN, _MIN_INNER_WIDTH)

            # Bottom stack is fixed; the conversation takes everything left.
            buttons_y = height - _MARGIN - _BUTTON_HEIGHT
            composer_y = buttons_y - _GAP - _COMPOSER_HEIGHT
            status_y = composer_y - _GAP - _STATUS_HEIGHT
            transcript_y = _MARGIN
            transcript_h = max(status_y - _GAP - transcript_y, 60)

            self._place("txtTranscript", _MARGIN, transcript_y, inner, transcript_h)
            # Bar on the left, label filling the rest of the row.
            bar_w = min(90, max(inner // 4, 40))
            self._place("prgStatus", _MARGIN, status_y + 4, bar_w, _STATUS_HEIGHT - 8)
            self._place("lblStatus", _MARGIN + bar_w + _GAP, status_y,
                        max(inner - bar_w - _GAP, 20), _STATUS_HEIGHT)
            self._place("txtComposer", _MARGIN, composer_y, inner, _COMPOSER_HEIGHT)
            half = max((inner - _GAP) // 2, 30)
            self._place("btnSend", _MARGIN, buttons_y, half, _BUTTON_HEIGHT)
            self._place("btnClear", _MARGIN + half + _GAP, buttons_y,
                        inner - half - _GAP, _BUTTON_HEIGHT)
            _log("%slayout: parent=%dx%d container=%dx%d inner=%d transcript_h=%d"
                 % ("re" if resized else "", psize.Width, psize.Height,
                    width, height, inner, transcript_h))
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

    def _entries(self):
        """The conversation, as [{kind, text}] — one entry per message.

        Kept structured rather than as flat lines so the renderer can lay out a
        conversation instead of a wall of text, and so a progress line can be
        replaced in place rather than appended.
        """
        return _TRANSCRIPTS.setdefault(
            _doc_key(self.frame),
            [{"kind": "cowork", "text": _greeting(self.frame)}])

    def _render(self):
        """Redraw the conversation, scrolled to the newest message."""
        control = self._control_by_name("txtTranscript")
        if control is None:
            return
        chunks = []
        for entry in self._entries():
            kind, text = entry["kind"], entry["text"]
            if kind == "you":
                chunks.append("You:\n%s" % text)
            elif kind == "cowork":
                chunks.append("Cowork:\n%s" % text)
            elif kind == "status":
                # Labelled like a turn so it does not read as stray text, but
                # marked as provisional.
                chunks.append("Cowork: %s" % text)
            else:
                chunks.append(text)
        body = "\n\n".join(chunks)
        try:
            control.setText(body)
            try:
                control.setSelection(
                    uno.createUnoStruct("com.sun.star.awt.Selection", 0, len(body)))
            except Exception:
                pass
        except Exception:
            _log("setText failed:\n%s" % traceback.format_exc())

    def _append(self, speaker, message):
        kind = {"You": "you", "Cowork": "cowork"}.get(speaker, "cowork")
        entries = self._entries()
        entries.append({"kind": kind, "text": message})
        if len(entries) > 200:
            del entries[:-200]
        self._render()

    # -- the progress row ---------------------------------------------- //

    def _spinner_value(self):
        """A slowly advancing fraction of the bar.

        The bar is deliberately NOT a percentage: nobody knows how far through a
        model turn is, and a bar that sits at 70% for a minute is worse than no
        bar. A slow crawl reads as "alive" without promising a finish time.
        """
        if not self._turn_started:
            return 30
        elapsed = time.time() - self._turn_started
        return int(15 + (elapsed * 3) % 70)

    def _status_label(self):
        """`Deep diving... 18s` — the label plus a clock, as DSH shows it."""
        text = self._status_text or "Working…"
        if self._turn_started:
            seconds = int(time.time() - self._turn_started)
            if seconds * 1000 >= _CLOCK_AFTER_MS:
                if seconds < 60:
                    text = "%s  %ds" % (text, seconds)
                else:
                    text = "%s  %d:%02d" % (text, seconds // 60, seconds % 60)
        return text

    def _refresh_status_row(self):
        label = self._control_by_name("lblStatus")
        bar = self._control_by_name("prgStatus")
        if label is not None:
            try:
                label.setText(self._status_label())
            except Exception:
                _log(traceback.format_exc())
        if bar is not None:
            try:
                bar.getModel().ProgressValue = self._spinner_value() if self._busy else 0
            except Exception:
                _log(traceback.format_exc())

    def _set_status(self, text):
        """Show what is happening in the progress row.

        Interim updates are evidence that work is happening, not conversation.
        They go in the progress row rather than the transcript, so the exchange
        is never buried under a list of "Reading…", "Checking…", "Running…".
        """
        self._status_text = text
        self._refresh_status_row()

    def _drop_status(self):
        """Clear the progress row; the turn has produced real output."""
        self._status_text = ""
        self._refresh_status_row()

    # -- the ticker ---------------------------------------------------- //

    def _start_ticker(self):
        """Begin the elapsed clock, if the toolkit offers a timer.

        Without one the clock never appears, but the animated progress bar still
        shows motion, so the panel is never silent about being busy.
        """
        self._stop_ticker()
        self._turn_started = time.time()
        self._refresh_status_row()
        try:
            ticker = self.ctx.ServiceManager.createInstanceWithContext(
                "com.sun.star.awt.Timer", self.ctx)
        except Exception:
            _log("no timer available; the elapsed clock stays hidden")
            return
        try:
            ticker.SetTimeout = 1000
            ticker.SetInterval = True
            ticker.SetCallback(_Ticker(self))
            ticker.Start()
            self._ticker = ticker
        except Exception:
            _log("timer setup failed:\n%s" % traceback.format_exc())

    def _stop_ticker(self):
        self._turn_started = 0.0
        if self._ticker is not None:
            try:
                self._ticker.Stop()
            except Exception:
                pass
            self._ticker = None

    def _set_busy(self, busy, label="Send"):
        """Reflect that a turn is running.

        A spinner is not available in this toolkit, so the Send button becomes
        the indicator: a slow turn with no visible change is indistinguishable
        from a frozen application.
        """
        """Reflect that a turn is running.

        The button label is the only affordance this panel has for 'working';
        without it a slow turn looks like a broken one.
        """
        self._busy = busy
        if busy:
            self._start_ticker()
        else:
            self._stop_ticker()
            self._status_text = ""
        self._refresh_status_row()
        button = self._controls.get("btnSend")
        if button is None:
            return
        try:
            button.getModel().Label = "Working…" if busy else label
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
        """Apply one worker event. Runs on the GUI thread only."""
        kind = item[0]
        _log("delivering %r on %s" % (kind, threading.current_thread().name))
        if kind == "chunk":
            piece = item[1]
            if not piece:
                return
            if not self._streaming:
                # First real output of a reply: the progress line has done its
                # job and makes way for the answer.
                self._drop_status()
                self._streaming = True
                self._entries().append({"kind": "cowork", "text": ""})
                self._render()
            self._entries()[-1]["text"] += piece
            self._render()
        elif kind == "tool":
            # A tool call interrupts the prose and reports progress in place.
            name = item[1]
            self._streaming = False
            self._last_tool = name
            self._set_status(self._describe_tool(name))
        elif kind == "final":
            self._drop_status()
            text = item[1]
            if not text:
                pass
            elif self._streaming:
                self._entries()[-1]["text"] = text
                self._render()
            else:
                self._append("Cowork", text)
            self._streaming = False
        elif kind == "error":
            self._drop_status()
            self._append("Cowork", item[1])
            self._streaming = False
        elif kind == "done":
            self._drop_status()
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
        _TRANSCRIPTS[_doc_key(self.frame)] = [
            {"kind": "cowork", "text": _greeting(self.frame)}]
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
