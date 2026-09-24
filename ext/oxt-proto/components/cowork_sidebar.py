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
import time
import traceback

import uno
import unohelper

from com.sun.star.awt import (XActionListener, XCallback, XKeyListener,
                              XAdjustmentListener, XWindowListener, XTextListener)
from com.sun.star.beans import PropertyAttribute
from com.sun.star.awt.Key import RETURN as KEY_RETURN
from com.sun.star.awt.KeyModifier import SHIFT as MOD_SHIFT
from com.sun.star.awt.PosSize import POSSIZE
from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel
from com.sun.star.ui import XSidebarPanel
from com.sun.star.ui.UIElementType import TOOLPANEL

# The client is plain Python with no UNO dependency, so the streaming path can
# be tested against a live service without a GUI.
try:
    import cowork_layout as layout
except ImportError:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import cowork_layout as layout

try:
    import cowork_markdown as markdown
except ImportError:  # the extension dir is not always on sys.path
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import cowork_markdown as markdown

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
# One rendered line, and the width of one character, in the container's units.
#
# These are 1/100 mm, and getting them wrong is invisible in code and obvious on
# screen: an earlier pair (62 and 60) meant a 10-point line was given 0.62 mm of
# height — text drawn on top of itself — and a 200-unit column allowed three
# characters, so the conversation rendered as scattered glyph fragments. Both
# numbers here are millimetres x 100 for 10-point text: a line needs roughly
# 3.5 mm plus leading, and a monospace advance is roughly 2.1 mm.
_ROW_HEIGHT = 340
_CHAR_WIDTH = 240


def _row_height(point_size):
    """Height for one line of text at a given point size, in 1/100 mm.

    Derived from the font rather than fixed. A single constant cannot fit a 9pt
    caption and a 12pt heading: too small and the lines overlap, too large and
    every message is padded with dead space, and both are invisible in code and
    obvious on screen. 10pt needs roughly 3.5mm of glyph plus 2mm of leading; the
    formula keeps that ratio at every size.
    """
    return int((point_size * 0.3528 + 2.0) * 100)

# Chat appearance, following Claude for Word's task pane: a plain background for
# the assistant's replies, a tinted bubble for the user's, generous padding, and
# the composer as a boxed field with a filled send button rather than a bare text
# field with two labelled buttons.
#
# CLAUDE-ish neutral palette. LibreOffice's dark theme supplies the rest, so these
# are only the few surfaces the panel owns.
_COLOR_BG = -1                 # -1 = the toolkit default background
_COLOR_USER_BUBBLE = 0x3A3A3A  # only used where it reads against the default bg
_COLOR_ASSISTANT = -1          # default text
_COLOR_MUTED = 0x9A9A9A
_COLOR_ACCENT = 0xC96442       # the send button's fill
_COLOR_ACCENT_TEXT = 0xFFFFFF

_MESSAGE_PAD = 90              # space around a message's text inside its bubble
_MESSAGE_GAP = 200             # space between two messages
# Real family names, taken from `fc-match` rather than from habit. The obvious
# guesses ("Liberation Sans" / "Liberation Mono") are only aliases on this
# platform; fc-match reports Noto Sans and DejaVu Sans Mono as what actually
# resolves, and a family the toolkit cannot find renders as scattered glyph
# fragments rather than falling back gracefully.
_MONO_FONT = "DejaVu Sans Mono"
_SANS_FONT = "Noto Sans"
# Show the elapsed clock only after this long, matching the DSH progress chrome:
# a count-up on a two-second call is noise, on a thirty-second one it is the
# difference between "working" and "hung".
# The elapsed count appears only after this long. A count-up on a two-second call
# is noise; on a thirty-second one it is the difference between "working" and
# "hung". Matches the harness's own progress chrome.
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
# The turn MUST NOT run on the VCL thread. Doing so froze the application hard
# enough that the desktop offered to force-quit LibreOffice — reported as "it
# responds but it then crashes the app".
#
# A worker produces events, a queue carries them, and `AsyncCallback` delivers
# them to the GUI thread. The re-arm is done by a small dedicated thread rather
# than from inside `notify`: a probe of both shapes in a live office showed each
# delivering four callbacks in a row, so re-entrant re-arming is not inherently
# broken — but the independent-thread shape cannot be affected by whatever else
# runs inside `notify`, and this is not a place to be clever twice.
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


def _doc_properties(frame):
    """The document's user-defined properties, which persist across save/open.

    LibreOffice stores custom properties in the file itself (the
    `meta:user-defined` elements in the ODF manifest), so a conversation saved
    here travels with the document — reopen it anywhere and the history is
    there. This is where the chat transcript belongs.
    """
    try:
        doc = frame.getController().getModel()
        return doc.getDocumentProperties().getUserDefinedProperties()
    except Exception:
        return None


def _load_conversation(frame):
    """Read the conversation from the document's metadata, if any."""
    import json as _json
    props = _doc_properties(frame)
    if props is None:
        return None
    try:
        stored = props.getPropertyValue("CoworkChat")
        entries = _json.loads(stored)
        if isinstance(entries, list):
            return entries
    except Exception:
        pass
    return None


def _save_conversation(frame, entries):
    """Write the conversation to the document's metadata.

    The document becomes "modified" by this write, so the user will be prompted
    to save and the history persists with the file. The property is REMOVEABLE
    so `clear` can drop it entirely.
    """
    import json as _json
    props = _doc_properties(frame)
    if props is None:
        return
    payload = _json.dumps(entries[-200:])          # bounded, matching the in-memory cap
    try:
        try:
            props.setPropertyValue("CoworkChat", payload)
        except Exception:
            props.addProperty("CoworkChat", PropertyAttribute.REMOVEABLE, payload)
    except Exception:
        _log("could not save the conversation to document metadata:\n%s"
             % traceback.format_exc())


def _clear_saved_conversation(frame):
    """Remove the stored conversation, when the user starts a new one."""
    props = _doc_properties(frame)
    if props is None:
        return
    try:
        props.removeProperty("CoworkChat")
    except Exception:
        pass


def _doc_name(frame):
    try:
        url = frame.getController().getModel().getURL()
        name = url.rsplit("/", 1)[-1] or "this untitled document"
        # A file URL percent-encodes spaces (%20) and other characters; the
        # sidebar should show the filename the user sees, not the escaped form.
        from urllib.parse import unquote
        return unquote(name)
    except Exception:
        return "this document"


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
            # Only if the conversation is still empty. A second panel instance
            # (a factory probe, or the sidebar rebuilding the deck) re-arms
            # _autosend_pending, and without this check it submits the same text
            # a second time and doubles the turn in the transcript.
            if element._entries():
                _log("autosend skipped: conversation already started")
            else:
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


def _turn_worker(element, prompt):
    """Run one turn off the GUI thread, reporting progress through the queue."""
    try:
        document = element.frame.getController().getModel().getURL() or ""
    except Exception:
        document = ""

    def on_event(kind, payload):
        if kind == "chunk":
            _emit(("chunk", payload.get("text", "")))
        elif kind == "tool":
            _emit(("tool", payload.get("label") or element._describe_tool(
                payload.get("name", ""))))

    try:
        final = AgentClient().ask(document, prompt, on_event,
                                  should_stop=lambda: element._closing)
        _emit(("final", final))
    except AgentUnavailable as exc:
        _emit(("error", str(exc)))
    except Exception as exc:  # noqa: BLE001
        _emit(("error", "That did not work: %s" % exc))


class _PumpRunner(threading.Thread):
    """Keeps asking the GUI thread to drain the queue.

    A dedicated thread rather than re-arming inside `notify`. Both shapes were
    measured delivering four callbacks in a row in a live office, so neither is
    obviously wrong; this one cannot be disturbed by anything else `notify` does,
    and the pump is the part that has already failed once.
    """

    def __init__(self, element):
        threading.Thread.__init__(self, name="cowork-pump", daemon=True)
        self.element = element

    def run(self):
        while not self.element._closing:
            self.element._arm_pump()
            time.sleep(0.15)


class _MainThreadPump(unohelper.Base, XCallback):
    """Runs on the GUI thread and drains the queue."""

    def __init__(self, element):
        self.element = element

    def notify(self, _data):
        element = self.element
        if element is None or element._closing:
            return
        element._pump_once()


class _ScrollListener(unohelper.Base, XAdjustmentListener):
    """Re-lays-out the transcript when the scrollbar moves.

    Registered with `addAdjustmentListener`: `UnoControlScrollBar` has no
    `addScrollListener`, and the interface is `XAdjustmentListener` rather than
    `XScrollListener` — which does not exist at all, and importing it makes the
    extension fail to load with "No module named 'com'".
    """

    def __init__(self, element):
        self.element = element

    def adjusted(self, event):
        element = self.element
        if element is None:
            return
        try:
            element._scroll_offset = int(event.Value)
            element._render()
        except Exception:
            _log(traceback.format_exc())

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
        self._closing = False
        self._streaming = False
        self._worker = None
        self._async = None
        self._pump = None
        self._pump_thread = None
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
        # The conversation is a stack of small styled labels inside a scrolling
        # container, not one edit control. A single control cannot render
        # markdown: its font properties apply to the whole control, and there is
        # no rich-text control available (UnoControlRichText returns None from
        # this context, like com.sun.star.awt.Timer). Per-control formatting is
        # what the toolkit does support, so each block becomes its own label.
        # The conversation is ONE text control.
        #
        # This replaced a stack of individually-positioned labels that could carry
        # per-run styling and chat bubbles. That approach cannot work here: the
        # sidebar hands the panel 232 units (2.32 cm, about 88 px, roughly fourteen
        # characters per line at 10pt), so a bubble's padding leaves six. Bubbles
        # are a fine idea that does not fit — which is why Claude for Word uses a
        # single plain text pane rather than bubbles.
        #
        # A text control also does, natively, the two things the label stack kept
        # getting wrong: it wraps at any width, and it scrolls.
        self._control(container, "txtTranscript", "UnoControlEdit",
                      "UnoControlEditModel",
                      MultiLine=True,
                      ReadOnly=True,
                      VScroll=True,
                      AutoVScroll=True,
                      Border=True,
                      HideInactiveSelection=False,
                      Tabstop=True)

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
        # AsyncCallback is creatable from this context; a bare script context
        # returns None, which is what made an earlier attempt look impossible.
        try:
            self._async = self.ctx.ServiceManager.createInstanceWithContext(
                "com.sun.star.awt.AsyncCallback", self.ctx)
            self._pump = _MainThreadPump(self)
            self._pump_thread = _PumpRunner(self)
            self._pump_thread.start()
            _log("pump started")
        except Exception:
            self._async = None
            self._pump = None
            _log("no AsyncCallback; progress will not update during a turn:\n%s"
                 % traceback.format_exc())

        autosend = os.environ.get("COWORK_AUTOSEND")
        if autosend:
            composer = self._controls.get("txtComposer")
            if composer is not None:
                composer.setText(autosend)
            _log("autosend armed: %r" % autosend)
            self._autosend_pending = True

        # Seed the conversation the first time this document is seen, so the
        # panel never opens blank.
        # No seeded greeting. The opening view is the empty state, which is
        # drawn rather than stored, so nothing has to be invalidated when its
        # wording changes — the earlier design cached a greeting per document and
        # kept showing stale text after the code moved on.
        _TRANSCRIPTS.setdefault(_doc_key(self.frame), [])
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

            self._transcript_width = inner
            self._transcript_view = transcript_h
            self._place("txtTranscript", _MARGIN, transcript_y, inner, transcript_h)
            self._place("lblStatus", _MARGIN, status_y, inner, _STATUS_HEIGHT)
            self._place("txtComposer", _MARGIN, composer_y, inner, _COMPOSER_HEIGHT)
            half = max((inner - _GAP) // 2, 30)
            self._place("btnSend", _MARGIN, buttons_y, half, _BUTTON_HEIGHT)
            self._place("btnClear", _MARGIN + half + _GAP, buttons_y,
                        inner - half - _GAP, _BUTTON_HEIGHT)
            _log("%slayout: parent=%dx%d container=%dx%d inner=%d transcript_h=%d"
                 % ("re" if resized else "", psize.Width, psize.Height,
                    width, height, inner, transcript_h))
            self._render()
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

        Persistent: the conversation is stored in the document's own metadata
        and reloaded when a document is reopened, so the exchange travels with
        the file. The in-store cache is only a per-session view.
        """
        key = _doc_key(self.frame)
        if key not in _TRANSCRIPTS:
            stored = _load_conversation(self.frame)
            if stored:
                _TRANSCRIPTS[key] = stored
        return _TRANSCRIPTS.setdefault(key, [])

    # -- rendering the conversation ------------------------------------ //

    # Block styling. Font heights are in points; the container works in the
    # window's units (1/100 mm), so heights are converted.
    _STYLE = {
        markdown.SPEAKER:   {"weight": 150.0, "size": 9,  "mono": False, "indent": 0},
        "note":             {"weight": 100.0, "size": 9,  "mono": False, "indent": 0},
        markdown.HEADING:   {"weight": 150.0, "size": 11, "mono": False, "indent": 0},
        markdown.PARAGRAPH: {"weight": 100.0, "size": 10, "mono": False, "indent": 0},
        markdown.BULLET:    {"weight": 100.0, "size": 10, "mono": False, "indent": 12},
        markdown.CODE:      {"weight": 100.0, "size": 9,  "mono": True,  "indent": 12},
        markdown.RULE:      {"weight": 100.0, "size": 8,  "mono": False, "indent": 0},
    }

    def _parsed(self, entry, index):
        """Markdown blocks for one message, parsed once and cached.

        Parsing means opening a hidden Writer document through LibreOffice's
        Markdown filter, which is far too expensive for every repaint — so the
        result is cached on the entry and invalidated only when its text changes.
        """
        text = entry.get("text") or ""
        cached = entry.get("_blocks")
        if cached is not None and entry.get("_parsed_from") == text:
            return cached
        try:
            blocks = markdown.parse_with_office(self.ctx, text)
        except Exception:
            _log("markdown parse failed; falling back to plain text:\n%s"
                 % traceback.format_exc())
            blocks = [{"kind": markdown.PARAGRAPH,
                       "runs": [{"text": text, "bold": False, "italic": False,
                                 "mono": False}]}]
        entry["_blocks"] = blocks
        entry["_parsed_from"] = text
        return blocks

    def _blocks(self):
        """Every message's blocks, with speaker labels between them."""
        blocks = []
        for entry in self._entries():
            parsed = self._parsed(entry, 0)
            # A chat does not label the assistant. Its replies are plain text and
            # the user's messages are the ones that need distinguishing, so the
            # speaker is carried on the block rather than rendered as a heading —
            # which is what Claude for Word does and what a transcript with
            # "Cowork:" above every reply does not.
            for block in parsed:
                block = dict(block)
                block["who"] = entry.get("kind")
                blocks.append(block)
        return blocks

    # -- the turn ------------------------------------------------------- //

    def submit(self):
        """Send the composer text as the next message in this conversation.

        The turn runs on a worker thread: running it here froze LibreOffice hard
        enough for the desktop to offer a force-quit.
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
            worker = threading.Thread(target=_turn_worker, args=(self, text),
                                      name="cowork-turn", daemon=True)
            self._worker = worker
            worker.start()
        except Exception as exc:  # noqa: BLE001
            _log("could not start the worker: %s" % exc)
            self._append("Cowork", "Could not start the request: %s" % exc)
            self._set_busy(False)

    def deliver(self, item):
        """Apply one worker event. Runs on the GUI thread only."""
        kind = item[0]
        if kind == "chunk":
            piece = item[1]
            if not piece:
                return
            if not self._streaming:
                self._streaming = True
                self._drop_status()
                self._entries().append({"kind": "cowork", "text": ""})
            self._entries()[-1]["text"] += piece
            self._render()
        elif kind == "tool":
            self._streaming = False
            self._set_status(item[1] or "Working…")
        elif kind == "final":
            was_streaming = self._streaming
            self._streaming = False
            self._set_busy(False)
            self._drop_status()
            entries = self._entries()
            # Persist to the document's metadata; the user will be prompted
            # to save, and the conversation then travels with the file.
            _save_conversation(self.frame, entries)
            # The chunks already filled the last cowork entry. Replace its text
            # with the authoritative final rather than appending a second copy —
            # appending doubled every reply in the transcript.
            if item[1] and was_streaming and entries \
                    and entries[-1]["kind"] == "cowork":
                entries[-1]["text"] = item[1]
            elif item[1]:
                entries.append({"kind": "cowork", "text": item[1]})
            self._render()
        elif kind == "error":
            was_streaming = self._streaming
            self._streaming = False
            self._set_busy(False)
            self._drop_status()
            entries = self._entries()
            if was_streaming and entries and entries[-1]["kind"] == "cowork":
                entries[-1]["text"] = item[1]
            else:
                entries.append({"kind": "cowork", "text": item[1]})
            self._render()
        elif kind == "pump":
            self._pump_once()

    def _pump_once(self):
        """Drain the queue onto this thread."""
        for item in _drain():
            try:
                self.deliver(item)
            except Exception:
                _log(traceback.format_exc())

    def _arm_pump(self):
        if self._async is None or self._pump is None:
            return
        try:
            self._async.addCallback(self._pump, None)
        except Exception:
            _log("addCallback failed:\n%s" % traceback.format_exc())

    def clear(self):
        """Start a fresh conversation for this document."""
        _clear_saved_conversation(self.frame)
        _TRANSCRIPTS[_doc_key(self.frame)] = []
        self._render()

    def _set_busy(self, busy, label="Send"):
        """Reflect that a turn is running, and run the elapsed clock.

        The toolkit has no spinner, so the Send button is the indicator: a slow
        turn with nothing changing is indistinguishable from a frozen application.
        """
        self._busy = busy
        if busy:
            self._turn_started = time.time()
        else:
            self._turn_started = 0.0
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

    # -- conversation state --------------------------------------------- //

    def _append(self, speaker, message):
        kind = {"You": "you", "Cowork": "cowork"}.get(speaker, "cowork")
        entries = self._entries()
        entries.append({"kind": kind, "text": message})
        if len(entries) > 200:
            del entries[:-200]
        self._render()

    # -- the status row -------------------------------------------------- //

    def _status_label(self):
        """`Checking the layout… 24s` — the label plus an elapsed clock.

        The clock appears only after 15 seconds, matching the harness's own
        progress chrome: a count-up on a two-second call is noise, on a
        thirty-second one it is the difference between working and hung.
        """
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
        """Draw the status line.

        Empty unless there is something to say. An earlier version fell back to
        "Working…" whenever the turn was busy, so clearing the status before
        clearing the busy flag redrew "Working…" and left it on screen after the
        work had finished.
        """
        label = self._control_by_name("lblStatus")
        if label is None:
            return
        try:
            label.setText(self._status_label() if self._status_text else "")
        except Exception:
            _log(traceback.format_exc())

    def _set_status(self, text):
        """Show what is happening, in the status row rather than the transcript.

        Interim updates are evidence that work is happening, not conversation, so
        they must not fill the pane and bury the exchange.
        """
        self._status_text = text
        self._refresh_status_row()

    def _drop_status(self):
        """Clear the status row; the turn has produced real output."""
        self._status_text = ""
        self._refresh_status_row()

    # Suggestions for the opening view. Claude for Word offers four chips that are
    # one click from a useful request; these are the same idea for a document.
    _SUGGESTIONS = (
        "Summarise this document",
        "Review it for problems",
        "Improve the wording",
        "What still needs doing?",
    )

    # -- rendering ------------------------------------------------------ //

    def _render_empty_state(self, control, inner, view):
        """The opening view, as text in the same control.

        Centred prose rather than positioned labels: the panel is 2.3cm wide, so a
        centred label is clipped on the left anyway, and a text control wraps.
        """
        # No hard line breaks mid-sentence: the control wraps visually, and every
        # inserted newline ends up in the clipboard when the user copies it.
        lines = [
            "How can I help with this document?",
            _doc_name(self.frame),
            "",
        ]
        lines += list(self._SUGGESTIONS)
        try:
            control.setText("\n".join(lines))
            control.setSelection(uno.createUnoStruct(
                "com.sun.star.awt.Selection", 0, 0))
        except Exception:
            _log(traceback.format_exc())

    def _render_messages(self, control, inner, view):
        """Write the conversation into the transcript as plain text.

        Geometry, wrapping and scrolling are the control's business here, which is
        the point: the label-stack renderer had to compute every line's position,
        width and height by hand and got it wrong repeatedly in ways only a
        screenshot revealed.
        """
        columns = max(int((inner - 20) / layout.CHAR_WIDTH), 8)
        body = layout.to_plain_text(self._blocks(), columns)
        try:
            control.setText(body)
            # A zero-width selection at the end is a caret; Selection(0, n) selects
            # everything and paints the whole conversation in the selection colour.
            control.setSelection(uno.createUnoStruct(
                "com.sun.star.awt.Selection", len(body), len(body)))
        except Exception:
            _log(traceback.format_exc())

    def _render(self):
        """Draw either the empty state or the conversation, into one text control."""
        control = self._control_by_name("txtTranscript")
        if control is None:
            return
        live = control.getPosSize()
        width = live.Width or self._transcript_width or _FALLBACK_WIDTH
        inner = max(width - 2 * _MARGIN, 60)
        if not self._entries():
            self._render_empty_state(control, inner, live.Height)
        else:
            self._render_messages(control, inner, live.Height)

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
        # Stop the pump before disposing controls: the worker may still be
        # running and must not touch a disposed window.
        self._closing = True
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
