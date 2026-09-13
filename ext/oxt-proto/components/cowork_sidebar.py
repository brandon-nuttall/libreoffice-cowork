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
import time
import traceback

import uno
import unohelper

from com.sun.star.awt import (XActionListener, XKeyListener, XWindowListener,
                              XTextListener)
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

# Bump when the greeting's wording changes. Cached conversations are keyed by
# document URL and survive code reloads, so without this a stale greeting is shown
# for every document that was ever opened. That is exactly what happened: the
# panel kept displaying an instruction to run a script that no longer exists.
_GREETING_VERSION = 2

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

    States what the agent is looking at and what it can do, then stops. It is not
    a question: an empty box with a question in it invites typing, whereas this
    reads as the top of a thread already in progress.
    """
    base = ("Working on %s. I can read it, edit it, restructure it, and check "
            "how the result looks — everything I change in one go is a single "
            "undo step." % _doc_name(frame))
    status = _service_status()
    if status is None:
        # Deliberately does NOT name a command to run. The panel starts the
        # runtime itself on the next message; an earlier version told the reader
        # to run a script that has since been deleted, and because the greeting
        # is cached per document that stale instruction survived a code change.
        return base + (" Send a message and I will start up. If nothing "
                       "happens, see ~/.cache/cowork-sidebar.log")
    if not status.get("runtime"):
        return base + " The Cowork runtime is running but not ready yet."
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
        element = self.element
        if element is None:
            return
        element.layout()
        if getattr(element, "_autosend_pending", False):
            element._autosend_pending = False
            _log("autosend firing submit()")
            try:
                element.submit()
            except Exception:
                _log(traceback.format_exc())

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


class _ComposerKeys(unohelper.Base, XKeyListener):
    """Shift+Enter sends; Enter inserts a newline.

    A multiline composer needs both, and the toolkit only gives the newline.

      * Shift+Enter is intercepted in `keyPressed`, CONSUMED, and the send is
        issued in `keyReleased`.
      * Plain Enter is left entirely alone, so the control inserts the newline
        and wrapping behaves like any other multiline field.

    Consuming rather than merely observing matters: letting Shift+Enter through
    would have the control append a newline and then the send fire, leaving a
    stray blank line in the composer.
    """

    def __init__(self, element):
        self.element = element
        self._pending_send = False

    def keyPressed(self, event):
        if self.element is None:
            return
        if event.KeyCode != KEY_RETURN:
            return
        modifiers = getattr(event, "Modifiers", 0) or 0
        if not (modifiers & MOD_SHIFT):
            return
        self._pending_send = True
        event.Consume()

    def keyReleased(self, event):
        if self.element is None or not self._pending_send:
            return
        self._pending_send = False
        if event.KeyCode == KEY_RETURN:
            self.element.submit()

    def disposing(self, _event):
        self.element = None


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

        self._autosend_pending = False
        # AsyncCallback + its callback object must both outlive every turn.
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

        _log("controls built: %s" % sorted(self._controls))

        # Testing hook: the control exposes only addKeyListener/removeKeyListener,
        # so a click cannot be fired through the UNO bridge and the panel's own
        # turn path — button to client to transport to transcript — was otherwise
        # unreachable without a person at the keyboard. With COWORK_AUTOSEND set
        # the panel sends that text once, shortly after it opens.
        autosend = os.environ.get("COWORK_AUTOSEND")
        if autosend:
            composer = self._controls.get("txtComposer")
            if composer is not None:
                composer.setText(autosend)
            _log("autosend armed: %r" % autosend)
            self._autosend_pending = True

        # Seed the conversation the first time this document is seen, so the
        # panel never opens blank.
        key = _doc_key(self.frame)
        cached = _TRANSCRIPTS.get(key)
        if cached is None or any(e.get("greeting") not in (None, _GREETING_VERSION)
                                 for e in cached):
            _TRANSCRIPTS[key] = [{"kind": "cowork",
                                  "text": _greeting(self.frame),
                                  "greeting": _GREETING_VERSION}]
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
            # Scroll to the newest message by placing a ZERO-WIDTH cursor at the
            # end: `Selection(n, n)` is a caret, `Selection(0, n)` is "select
            # everything" and paints the entire conversation in the selection
            # colour. The transcript was permanently highlighted because of that
            # one wrong first argument.
            try:
                control.setSelection(
                    uno.createUnoStruct("com.sun.star.awt.Selection", len(body), len(body)))
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

    def _start_ticker(self):
        """Mark when the turn began, for the elapsed label.

        There is no ticking timer: the turn runs inline, so a timer would not
        fire until it finished. The elapsed time is computed whenever the status
        row is redrawn, which happens on every tool call — and that is where the
        useful reading is ("Checking the layout… 24s").
        """
        self._turn_started = time.time()

    def _stop_ticker(self):
        self._turn_started = 0.0

    def _refresh_status_row(self):
        """Draw the progress row.

        The row is empty unless there is something to say. An earlier version fell
        back to "Working…" whenever the turn was busy, which meant that clearing
        the status *before* clearing the busy flag — the natural order at the end
        of a turn — redrew "Working…" and left it on screen after the work had
        finished. Verified in a capture: the reply was rendered and the row still
        read "Working…".

        So the label shows only what a tool call actually reported, and the bar
        only moves while a turn is running.
        """
        label = self._control_by_name("lblStatus")
        bar = self._control_by_name("prgStatus")
        if label is not None:
            try:
                label.setText(self._status_label() if self._status_text else "")
            except Exception:
                _log(traceback.format_exc())
        if bar is not None:
            try:
                value = self._spinner_value() if (self._busy and self._status_text) else 0
                bar.getModel().ProgressValue = value
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

    # -- the turn ------------------------------------------------------- //

    def submit(self):
        """Send the composer text as the next message in this conversation.

        The turn runs on this thread, deliberately.

        Streaming through `AsyncCallback` was tried and abandoned. The pump fired
        once and then stopped: re-arming a callback from inside `notify` is
        silently dropped when the same `notify` also drains a queue, which is
        exactly what streaming needs. A probe that re-armed from `notify` with
        nothing else to do worked, so the failure is specific to that shape.

        A turn that silently stops updating is worse than one that blocks, so the
        reply is rendered as it arrives on this thread and the progress row plus
        the Send button carry the "still working" signal.
        """
        composer = self._control_by_name("txtComposer")
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
        try:
            self._run_turn(text)
        finally:
            if self._busy:
                self._set_busy(False)

    def _run_turn(self, prompt):
        """Stream one reply into the transcript, on this thread."""
        try:
            document = self.frame.getController().getModel().getURL() or ""
        except Exception:
            document = ""

        state = {"streaming": False}

        def on_event(kind, payload):
            if kind == "chunk":
                piece = payload.get("text", "")
                if not piece:
                    return
                if not state["streaming"]:
                    state["streaming"] = True
                    self._drop_status()
                    self._entries().append({"kind": "cowork", "text": ""})
                self._entries()[-1]["text"] += piece
                self._render()
            elif kind == "tool":
                # Progress belongs in the row, never in the conversation.
                state["streaming"] = False
                label = payload.get("label") or self._describe_tool(
                    payload.get("name", ""))
                self._set_status(label)

        try:
            final = AgentClient().ask(document, prompt, on_event)
        except AgentUnavailable as exc:
            self._drop_status()
            self._append("Cowork", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._drop_status()
            self._append("Cowork", "That did not work: %s" % exc)
            return

        # Clear busy before the row so the row is not redrawn as "busy" one last
        # time by its own clearing.
        self._set_busy(False)
        self._drop_status()
        if final and state["streaming"]:
            self._entries()[-1]["text"] = final
        elif final:
            self._append("Cowork", final)
        self._render()

    def _set_busy(self, busy, label="Send"):
        """Reflect that a turn is running.

        The toolkit has no spinner, so the Send button is the indicator: a slow
        turn with nothing changing is indistinguishable from a frozen
        application. The progress row is refreshed on the same transition.
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

    def clear(self):
        """Start a fresh thread for this document."""
        _TRANSCRIPTS[_doc_key(self.frame)] = [
            {"kind": "cowork", "text": _greeting(self.frame),
             "greeting": _GREETING_VERSION}]
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
