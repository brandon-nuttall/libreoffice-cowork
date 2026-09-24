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
from com.sun.star.datatransfer import XTransferable
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
    import cowork_chat as chat
except ImportError:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import cowork_layout as layout
    import cowork_chat as chat

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

# Calibration fallbacks, in the same units the panel actually uses: the probes
# measured a 10pt line pitch of ~11 units and a ~5-unit top inset on this
# backend, where possize units track device pixels (see _calibrations).
_FALLBACK_PITCH = 13.0
_FALLBACK_TOP = 4.0
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
        raw = _json.loads(stored)
        if isinstance(raw, list):
            return [{"kind": e.get("kind", "cowork"),
                     "text": e.get("text", "")}
                    for e in raw if isinstance(e, dict)]
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
    # Strip the parse cache: only kind and text are conversation. Persisting
    # `_blocks`/`_parsed_from` bloated the document metadata and let a stale
    # cache survive a code change.
    slim = [{"kind": e.get("kind", "cowork"), "text": e.get("text", "")}
            for e in entries[-200:]]
    payload = _json.dumps(slim)
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


class _TextTransferable(unohelper.Base, XTransferable):
    """Carries plain text to the system clipboard.

    MUST implement XTransferable: setContents refuses anything that does not,
    with "value does not implement com.sun.star.datatransfer.XTransferable".
    The first version inherited only unohelper.Base and was caught by probing
    the clipboard service before shipping -- or the button would have done
    nothing at all.
    """

    def __init__(self, text):
        self._text = text

    def getTransferData(self, flavor):
        if str(flavor.MimeType).startswith("text/plain"):
            return self._text
        raise uno.IllegalArgumentException("unsupported flavor", None)

    def getTransferDataFlavors(self):
        flavor = uno.createUnoStruct("com.sun.star.datatransfer.DataFlavor")
        flavor.MimeType = "text/plain;charset=utf-8"
        flavor.HumanPresentableName = "Plain text"
        return (flavor,)

    def isDataFlavorSupported(self, flavor):
        return str(flavor.MimeType).startswith("text/plain")


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
            element._on_scroll(int(event.Value))
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
        # Transcript state: the control pool, the observed line metrics and the
        # cached stack so a user scroll never re-measures (docs/CHAT_UI_DESIGN.md).
        # Initialised in __init__ so the first render cannot touch an attribute
        # that does not exist yet — the _transcript_bubbles crash of F51.
        self._pnl = None
        self._used_offset = 0
        self._transcript_width = 0
        self._transcript_view = 0
        self._transcript_pool = []
        self._fillers = []
        self._chrome_fillers = []
        self._chrome_fillers = []
        self._line_pitch = 0.0
        self._line_top = 0.0
        self._autoscroll = True
        self._plan = None
        self._stack = []
        self._scroll_offset = 0
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
        # The conversation is a scrolling container of one MultiLine label per
        # message block (docs/CHAT_UI_DESIGN.md). The toolkit wraps each label
        # natively (probed: 6 rendered lines for a long label at width 220) and
        # reports its rendered line geometry through the accessible interface, so
        # heights are OBSERVED from the render rather than predicted from a
        # character-width model — the mistake that sank the previous bubble
        # design. FixedText is not text-selectable, which the Copy button
        # compensates for.
        self._control(container, "pnlTranscript", "UnoControlContainer",
                      "UnoControlContainerModel")
        # The whole panel is one canvas in OUR palette (contrast owned, not the
        # theme's): pane, chrome and composer all paint their own colour.
        try:
            container.getModel().BackgroundColor = chat.PANEL_BG
            self._control_by_name("pnlTranscript").getModel().BackgroundColor = (
                chat.PANEL_BG)
            self._pnl_parent = container
        except Exception:
            _log("painting the canvas failed:\n%s" % traceback.format_exc())


        self._control(container, "scrTranscript", "UnoControlScrollBar",
                      "UnoControlScrollBarModel",
                      Orientation=1,      # VERTICAL
                      ScrollValue=0, ScrollValueMax=0, BlockIncrement=40,
                      LineIncrement=11)
        # THE ADJUSTMENT LISTENER IS NOT OPTIONAL. The first bubble build
        # relayed the scrollbar into existence and never re-attached the
        # listener the previous design had, so dragging did literally nothing
        # and every render re-pinned the view to the bottom — the start of the
        # conversation was unreachable.
        bar = self._control_by_name("scrTranscript")
        if bar is not None:
            scroll_listener = _ScrollListener(self)
            try:
                bar.addAdjustmentListener(scroll_listener)
                self._listeners.append(scroll_listener)
                _log("scrollbar adjustment listener attached")
            except Exception:
                _log("could not attach the scrollbar listener:\n%s"
                     % traceback.format_exc())

        self._control(container, "lblStatus", "UnoControlFixedText",
                      "UnoControlFixedTextModel",
                      Label="", MultiLine=False, Align=0)

        # The composer is a flat field ON the canvas (Claude-style pill), not a
        # bordered box floating outside it.
        self._control(container, "txtComposer", "UnoControlEdit",
                      "UnoControlEditModel",
                      MultiLine=True,
                      Border=False,
                      VScroll=True,
                      AutoVScroll=True,
                      HScroll=False,
                      AutoHScroll=False,
                      BackgroundColor=chat.COMPOSER_BG)

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
        for name, label in (("btnSend", "Send"), ("btnClear", "Clear"),
                            ("btnCopy", "Copy")):
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
        # NOTE: deliberately no _TRANSCRIPTS.setdefault here. Seeding an empty
        # list before the lazy loader runs would put the document's key in the
        # table, and _entries() only consults the persisted conversation when
        # the key is missing — the poisoned seed is why a saved chat was never
        # reloaded.
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
            inner_w = max(inner - chat.BAR_W - chat.BAR_GAP, 60)
            self._place("pnlTranscript", _MARGIN, transcript_y, inner_w,
                        transcript_h)
            self._place("scrTranscript", _MARGIN + inner_w + chat.BAR_GAP,
                        transcript_y, chat.BAR_W, transcript_h)
            self._place("lblStatus", _MARGIN, status_y, inner, _STATUS_HEIGHT)
            send_w = 44
            self._place("btnSend", _MARGIN + inner - send_w - 4,
                        composer_y + 4, send_w, _COMPOSER_HEIGHT - 8)
            self._place("txtComposer", _MARGIN, composer_y,
                        inner - send_w - 10, _COMPOSER_HEIGHT)
            half = max((inner - _GAP) // 2, 24)
            self._place("btnClear", _MARGIN, buttons_y, half, _BUTTON_HEIGHT)
            self._place("btnCopy", _MARGIN + half + _GAP, buttons_y,
                        inner - half - _GAP, _BUTTON_HEIGHT)
            self._paint_composer_chrome(margin_x=_MARGIN, y=composer_y,
                                        w=inner, h=_COMPOSER_HEIGHT)
            self._relayout_transcript()
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

    def _paint_composer_chrome(self, margin_x, y, w, h):
        """The composer pill: ring strips + pane-coloured corner cuts."""
        if self._pnl_parent is None:
            return
        bg = chat.COMPOSER_BG
        ring = chat.PANEL_BG
        index = 0

        def rect(x, yy, ww, hh, colour):
            nonlocal index
            ctl = self._chrome(index)
            ctl.getModel().BackgroundColor = colour
            ctl.setPosSize(x, yy, max(ww, 1), max(hh, 1), POSSIZE)
            ctl.setVisible(True)
            index += 1

        rect(margin_x, y, w, 4, bg)
        rect(margin_x, y + h - 4, w, 4, bg)
        rect(margin_x, y, 6, h, bg)
        rect(margin_x + w - 6, y, 6, h, bg)
        R = 3
        for cx, cy, dx, dy in ((margin_x, y, 1, 1),
                               (margin_x + w, y, -1, 1),
                               (margin_x, y + h, 1, -1),
                               (margin_x + w, y + h, -1, -1)):
            x0 = cx if dx > 0 else cx - R
            y0 = cy if dy > 0 else cy - R
            rect(x0, y0, R, 1, ring)
            rect(x0, y0, 1, R, ring)
        lbl = self._control_by_name("lblStatus")
        if lbl is not None:
            try:
                lbl.getModel().BackgroundColor = chat.PANEL_BG
            except Exception:
                pass

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

        Parsing is one pure in-process function (`cowork_markdown.parse`) --
        rendered markdown must never crash, so there are no secondary paths to
        fail -- while the per-turn scheduling below keeps the per-chunk render
        cheap. (The previous design parsed through a hidden LibreOffice
        document; it produced hundreds of crashes a session and a tower of
        fallbacks. It is gone.)
        """
        text = entry.get("text") or ""
        cached = entry.get("_blocks")
        if cached is not None and entry.get("_parsed_from") == text:
            return cached
        # While text is streaming in, chunk lengths change every render; PARSE
        # ONLY ONCE, when the turn ends. The mid-stream view is a plain
        # paragraph (this function returns unstyled blocks then), which also
        # keeps a half-written reply from flashing structured styling that
        # changes as the words arrive.
        if getattr(self, "_streaming", False) and entry is self._entries()[-1]:
            entry["_blocks"] = [{"kind": markdown.PARAGRAPH,
                                 "runs": [{"text": text, "bold": False,
                                           "italic": False, "mono": False}]}]
            entry["_parsed_from"] = text
            return entry["_blocks"]
        blocks = markdown.parse(text)
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

        self._autoscroll = True
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
            self._autoscroll = True
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
            self._autoscroll = True
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
            self._autoscroll = True
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

    # -- transcript rendering (chat bubbles; see docs/CHAT_UI_DESIGN.md) --- //

    def _blocks_for_display(self):
        if self._entries():
            return self._blocks()
        # The empty state rides the same pipeline as a conversation: one block
        # per line, centred. Two code paths for one visual would double the
        # places a layout bug can live.
        doc = _doc_name(self.frame)
        blocks = [
            {"kind": chat.CENTER, "who": "cowork",
             "runs": [{"text": "How can I help with this document?",
                       "bold": True, "italic": False, "mono": False}]},
            {"kind": chat.CENTER_MUTED, "who": "cowork",
             "runs": [{"text": doc, "bold": False, "italic": False,
                       "mono": False}]},
        ]
        for suggestion in self._SUGGESTIONS:
            blocks.append({"kind": chat.CENTER, "who": "cowork",
                           "runs": [{"text": suggestion, "bold": False,
                                     "italic": False, "mono": False}]})
        return blocks

    # The block pool is repainted every render and stale entries are hidden;
    # the CHROME pool is painted once per layout (composer pill, its corners)
    # and never hidden -- two pools so transcript redraws cannot eat chrome.
    def _chrome(self, index):
        pool = self._chrome_fillers
        while len(pool) <= index:
            ctl = self._new("UnoControlFixedText")
            model = self._new("UnoControlFixedTextModel")
            model.Label = ""
            ctl.setModel(model)
            try:
                self._pnl_parent.addControl("chrome%d" % len(pool), ctl)
            except Exception:
                _log("chrome addControl failed:\n%s" % traceback.format_exc())
            pool.append(ctl)
        return pool[index]

    def _rect(self, index, x, y, w, h, colour):
        """Paint one colour rectangle from the block pool."""
        ctl = self._filler(index)
        ctl.getModel().BackgroundColor = colour
        ctl.setPosSize(x, y, max(w, 1), max(h, 1), POSSIZE)
        ctl.setVisible(True)

    def _filler(self, index):
        """Seam strips between same-speaker blocks (colour-matched)."""
        pool = self._fillers
        while len(pool) <= index:
            ctl = self._new("UnoControlFixedText")
            model = self._new("UnoControlFixedTextModel")
            model.Label = ""
            ctl.setModel(model)
            try:
                self._pnl.addControl("fill%d" % len(pool), ctl)
            except Exception:
                _log("filler addControl failed:\n%s" % traceback.format_exc())
            pool.append(ctl)
        return pool[index]

    def _pool_slot(self, index):
        """One label control per slot, created on demand and reused."""
        pool = self._transcript_pool
        while len(pool) <= index:
            ctl = self._new("UnoControlFixedText")
            model = self._new("UnoControlFixedTextModel")
            model.MultiLine = True
            ctl.setModel(model)
            try:
                self._pnl.addControl("blk%d" % len(pool), ctl)
            except Exception:
                _log("addControl failed:\n%s" % traceback.format_exc())
            pool.append(ctl)
        return pool[index]

    def _calibrations(self):
        """Line pitch + top inset, measured from a two-line label.

        The label trick is: two words at a width that fits neither, so the
        toolkit MUST wrap to two lines. (A literal newline in `Label` is NOT a
        line break on this backend — the first calibration attempt measured the
        line-feed glyph's box instead and produced a negative pitch.)

        Units note, from the probes: on this backend one possize unit tracks a
        device pixel (~72 dpi), so a 10pt line is about 11-13 units, NOT the
        550-ish units the 1/100 mm interpretation of the same numbers suggests.
        Measure-by-render sidesteps the question; the fallback below is simply
        the probe's observation, and a failed calibration is retried each render
        rather than cached.
        """
        if self._line_pitch:
            return self._line_pitch, self._line_top
        if not self._pnl:
            return _FALLBACK_PITCH, _FALLBACK_TOP
        try:
            ctl = self._pool_slot(len(self._transcript_pool))
            model = ctl.getModel()
            model.FontName = _SANS_FONT
            model.FontHeight = 10
            acc = ctl.getAccessibleContext()
            for width in (16, 20, 26):
                model.Label = "pp pp pp"
                ctl.setPosSize(0, -4000, width, 200, POSSIZE)
                n = acc.getCharacterCount()
                if n < 3:
                    continue
                a = acc.getCharacterBounds(0)
                b = acc.getCharacterBounds(n - 1)
                pitch = float(b.Y - a.Y)
                if pitch > 4 and pitch < 60 and b.Y > a.Y:
                    self._line_pitch = pitch
                    self._line_top = float(a.Y)
                    model.Label = ""
                    ctl.setPosSize(0, -4000, 1, 1, POSSIZE)
                    _log("calibrated: pitch=%.1f top=%.1f (width=%d)"
                         % (pitch, self._line_top, width))
                    return self._line_pitch, self._line_top
            raise ValueError("no plausible pitch at any probe width")
        except Exception:
            _log("calibration retrying later (fallback this render):\n%s"
                 % traceback.format_exc())
        return _FALLBACK_PITCH, _FALLBACK_TOP

    def _relayout_transcript(self):
        """Full measure + place after the panel size changed.

        Wrapping depends on width, so a resize invalidates the cached stack —
        this is the only path back through the measure passes.
        """
        self._stack = []
        self._plan = None
        self._render()

    def _render(self):
        """Redraw the transcript: one MultiLine label per message block."""
        if self._pnl is None:
            self._pnl = self._control_by_name("pnlTranscript")
            if self._pnl is None:
                return
        try:
            panel = self._pnl.getPosSize()
            if not panel.Width:          # layout() has not placed us yet
                return
            width = panel.Width or self._transcript_width or _FALLBACK_WIDTH
            view = panel.Height or self._transcript_view or 400
            blocks = self._blocks_for_display()
            pitch, top = self._calibrations()

            plan_rows = []
            rendered = []
            prev = None
            y = 0
            for index, block in enumerate(blocks):
                st = chat.style_for(block)
                text = "".join(r.get("text", "")
                               for r in block.get("runs", [])).strip()
                if block.get("kind") == chat.RULE:
                    text = ""
                gap = chat.gap_between(prev, block) if prev is not None else 0
                ctl = self._pool_slot(index)
                try:
                    model = ctl.getModel()
                    model.Label = chat.bind_dashes(st.get("prefix", "") + text)
                    model.MultiLine = True
                    model.FontName = _MONO_FONT if st["mono"] else _SANS_FONT
                    model.FontHeight = 10 + st["size_delta"]
                    model.FontWeight = st["weight"]
                    model.Align = st["align"]
                    model.TextColor = st["fg"]
                    model.BackgroundColor = st["bg"]
                except Exception:
                    _log("configure failed:\n%s" % traceback.format_exc())

                x = int(width * st["x_frac"])
                w = max(int(width * (1 - st["x_frac"] - st["right_frac"])) - 2, 20)
                if st["observed"]:
                    # Pass 1: generous height, final width and position, so the
                    # toolkit wraps at the real width.
                    ctl.setPosSize(x, y + gap, w, 800, POSSIZE)
                    h = None
                    try:
                        acc = ctl.getAccessibleContext()
                        n = acc.getCharacterCount()
                        if n:
                            last = acc.getCharacterBounds(n - 1)
                            lines = round((last.Y - top) / pitch) + 1
                            h = int(top + lines * pitch + st["pad_b"])
                    except Exception:
                        _log("measure failed:\n%s" % traceback.format_exc())
                    if h is None or h <= 0:
                        # Fallback: estimate from wrapped-line count at the
                        # observed pitch, assuming ~1 line per 12 chars.
                        h = int(top + (len(text) // 12 + 1) * pitch
                                + st["pad_b"])
                else:
                    h = st["fixed_h"]

                plan_rows.append({"key": "b%d" % index, "x": x,
                                  "right": st["right_frac"],
                                  "who": block.get("who"), "h": h,
                                  "gap": gap})
                ctl.setPosSize(x, y + gap, w, h, POSSIZE)
                rendered.append(ctl)
                y += gap + h
                prev = block

            total = y
            # PASS THE USER'S OFFSET. Passing None unconditionally -- what this
            # line did before -- re-pinned the view to the bottom on every
            # render, which during streaming meant many times a second: the
            # scrollbar "didn't work" because drags were overwritten within
            # milliseconds.
            offset = None if self._autoscroll else self._scroll_offset
            self._plan = chat.plan(plan_rows, view, offset)
            self._stack = plan_rows
            used = self._plan["used_offset"]
            self._used_offset = used
            fi = 0                      # block-filler cursor for this pass
            previous = None
            R = 3                       # rounded-corner cut, in units
            OUTER = chat.PANEL_BG
            for index, row in enumerate(self._plan["rows"]):
                ctl = rendered[index]
                if index >= len(rendered):
                    break
                if row["visible"]:
                    who = row.get("who")
                    if previous is None or previous.get("who") != who:
                        run_top = row["y"] - used - chat.PAD_V
                    y = row["y"] - used
                    bubble_colour = (chat.USER_BG if who == "you"
                                     else chat.MODEL_BG)
                    px = row["x"]
                    full_w = int(width * (1 - row.get("right", 0)) - row["x"]) - 2
                    bx_right = px + full_w
                    text_w = max(full_w - 2 * chat.PAD_H, 20)

                    ctl.setPosSize(px + chat.PAD_H, y, text_w, row["h"], POSSIZE)
                    ctl.setVisible(True)

                    if previous is not None and previous.get("who") == who:
                        gap_top = previous["y"] + previous["h"] - used
                        seam = (y - chat.PAD_V) - gap_top
                        if seam > 2:
                            self._rect(fi, px, gap_top + 1, full_w, seam - 2,
                                       bubble_colour)
                            fi += 1

                    # left / right rings (the text is inset inside the bubble)
                    self._rect(fi, px, y, chat.PAD_H, row["h"], bubble_colour); fi += 1
                    self._rect(fi, px + chat.PAD_H + text_w, y,
                               max(full_w - chat.PAD_H - text_w, 4), row["h"],
                               bubble_colour)
                    fi += 1
                    # top / bottom strips
                    self._rect(fi, px, y - chat.PAD_V, full_w, chat.PAD_V,
                               bubble_colour)
                    fi += 1
                    self._rect(fi, px, y + row["h"], full_w,
                               chat.PAD_V_BOTTOM, bubble_colour)
                    fi += 1

                    # ROUNDED OUTER CORNERS: pane-coloured covers on the very
                    # outside of each turn -- the run's first row gets two at
                    # the top, and the run's last row two at the bottom. The
                    # outer silhouette is cut by R on each corner while the
                    # inside stays rectangular.
                    if previous is None or previous.get("who") != who:
                        top_y = y - chat.PAD_V
                        self._rect(fi, px, top_y, R, 1, OUTER); fi += 1
                        self._rect(fi, px, top_y, 1, R, OUTER); fi += 1
                        self._rect(fi, bx_right - R, top_y, R, 1, OUTER); fi += 1
                        self._rect(fi, bx_right - 1, top_y, 1, R, OUTER); fi += 1
                    previous = row
                else:
                    if previous is not None:
                        # close the turn at the previous visible row: bottom
                        # corner covers (the pane gets the last word at the
                        # outer corners)
                        pw = max(int(width * (1 - previous.get("right", 0))
                                     - previous["x"]) - 2, 20)
                        px_o = previous["x"]
                        bot_y = (previous["y"] - used + previous["h"]
                                 + chat.PAD_V_BOTTOM)
                        self._rect(fi, px_o, bot_y - 1, R, 1, OUTER); fi += 1
                        self._rect(fi, px_o, bot_y - R, 1, R, OUTER); fi += 1
                        self._rect(fi, px_o + pw - R, bot_y - 1, R, 1, OUTER); fi += 1
                        self._rect(fi, px_o + pw - 1, bot_y - R, 1, R, OUTER); fi += 1
                    previous = None
                    ctl.setVisible(False)
            # a run that reaches the pane's end still gets bottom covers
            if previous is not None:
                pw = max(int(width * (1 - previous.get("right", 0))
                             - previous["x"]) - 2, 20)
                px_o = previous["x"]
                bot_y = (previous["y"] - used + previous["h"]
                         + chat.PAD_V_BOTTOM)
                self._rect(fi, px_o, bot_y - 1, R, 1, OUTER); fi += 1
                self._rect(fi, px_o, bot_y - R, 1, R, OUTER); fi += 1
                self._rect(fi, px_o + pw - R, bot_y - 1, R, 1, OUTER); fi += 1
                self._rect(fi, px_o + pw - 1, bot_y - R, 1, R, OUTER); fi += 1
            for stale in self._fillers[fi:]:
                try:
                    stale.setVisible(False)
                except Exception:
                    pass
            # Hide pool entries this render did not use.
            for stale in self._transcript_pool[len(rendered):]:
                try:
                    stale.setVisible(False)
                except Exception:
                    pass
            for stale in self._fillers[fi:]:
                try:
                    stale.setVisible(False)
                except Exception:
                    pass

            bar = self._control_by_name("scrTranscript")
            if bar is not None:
                try:
                    bar.setValues(used, min(view, max(total, 1)),
                                  max(total, 1))
                except Exception:
                    try:
                        model = bar.getModel()
                        model.ScrollValueMax = max(self._plan["max_offset"], 0)
                        model.ScrollValue = used
                    except Exception:
                        _log("scrollbar set failed:\n%s"
                             % traceback.format_exc())
        except Exception:
            _log("render failed:\n%s" % traceback.format_exc())

    def _on_scroll(self, value):
        """Reposition cached rows for a user scroll: arithmetic only.

        Measured heights are cached in `self._stack`, so a scroll tick costs a
        plan + a sweep of setPosSize calls, never a re-measure. Dragging also
        cancels autoscroll: the person is in charge until the next turn starts.
        """
        self._autoscroll = False
        self._scroll_offset = value
        self._used_offset = value
        if not self._stack:
            self._render()
            return
        try:
            panel = self._pnl.getPosSize()
            width = panel.Width or self._transcript_width or _FALLBACK_WIDTH
            view = panel.Height or self._transcript_view or 400
            refreshed = chat.plan(self._stack, view, value)
            pool = self._transcript_pool
            for index, row in enumerate(refreshed["rows"]):
                if index >= len(pool):
                    break
                ctl = pool[index]
                if row["visible"]:
                    ctl.setPosSize(row["x"], row["y"] - refreshed["used_offset"],
                                   max(int(width * (1 - row.get("right", 0)) - row["x"]) - 2, 20),
                                   row["h"], POSSIZE)
                    ctl.setVisible(True)
                else:
                    ctl.setVisible(False)
            bar = self._control_by_name("scrTranscript")
            if bar is not None:
                try:
                    bar.setValues(refreshed["used_offset"],
                                  min(view, refreshed["total"]),
                                  max(refreshed["total"], 1))
                except Exception:
                    pass
        except Exception:
            _log("scroll apply failed:\n%s" % traceback.format_exc())

    def _copy_transcript(self):
        """Put the whole conversation on the clipboard as clean text."""
        try:
            blocks = self._blocks()
            text = layout.to_plain_text(blocks, 200)
            if not text.strip():
                return
            clip = self.ctx.ServiceManager.createInstanceWithContext(
                "com.sun.star.datatransfer.clipboard.SystemClipboard", self.ctx)
            transferable = _TextTransferable(text)
            clip.setContents(transferable, None)
            # Deliberately NOT dropped: the note stays until the next turn
            # starts, so the user actually sees the confirmation.
            self._set_status("Copied.")
        except Exception:
            _log("copy failed:\n%s" % traceback.format_exc())

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
        self._pnl = None
        self._used_offset = 0
        self._transcript_width = 0
        self._transcript_view = 0
        self._transcript_pool = []
        self._plan = None
        self._stack = []
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
