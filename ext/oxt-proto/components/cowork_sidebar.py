"""Cowork sidebar panel — the visible agent surface inside LibreOffice.

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

The transcript is kept in a module-level store keyed by document URL, so it
survives the sidebar tearing the panel down and rebuilding it (switching decks,
reloading a document).
"""

import os
import traceback

import uno
import unohelper

from com.sun.star.awt import XActionListener, XWindowListener, XTextListener
from com.sun.star.awt.PosSize import POSSIZE
from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel
from com.sun.star.ui import XSidebarPanel
from com.sun.star.ui.UIElementType import TOOLPANEL

# MUST equal FactoryImplementation in Factories.xcu.
IMPL_NAME = "com.cowork.SidebarFactory"

_LOG = os.environ.get("COWORK_SIDEBAR_LOG",
                      "/home/brandon/opt/libreoffice-cowork/research/sidebar.log")


def _log(msg):
    try:
        with open(_LOG, "a") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass

_MARGIN = 6
_MIN_INNER_WIDTH = 80
_FALLBACK_WIDTH = 240

# Per-document conversation transcripts, keyed by document URL. Module-level so
# they outlive any individual panel instance.
_TRANSCRIPTS = {}
_DEFAULT_KEY = "(no document)"


def _doc_key(frame):
    """Identify the conversation by document, so each document keeps its own."""
    try:
        controller = frame.getController()
        model = controller.getModel()
        url = model.getURL()
        return url or _DEFAULT_KEY
    except Exception:
        return _DEFAULT_KEY


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


class _SendListener(unohelper.Base, XActionListener, XTextListener):
    """Send on button click, or on Enter in the composer."""

    def __init__(self, element):
        self.element = element

    def actionPerformed(self, _event):
        if self.element is not None:
            self.element.submit()

    def textChanged(self, _event):
        pass  # reserved: typing indicator / slash-command completion

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
        size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        size.Minimum = self._height
        size.Preferred = self._height
        size.Maximum = self._height
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

    def getRealInterface(self):
        if self._panel is None:
            root = self._build_window()
            self._panel = CoworkToolPanel(root, 260)
        return self._panel

    # -- construction -------------------------------------------------- //

    def _new(self, kind):
        return self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.awt.%s" % kind, self.ctx)

    def _control(self, container, name, kind, model_kind, **props):
        """Create one control defensively: a control that fails to build must
        not take the whole panel down with it."""
        try:
            control = self._new(kind)
            model = self._new(model_kind)
            for key, value in props.items():
                setattr(model, key, value)
            control.setModel(model)
            container.addControl(name, control)
            self._controls[name] = control
            _log("control OK: %s (%s)" % (name, kind))
            return control
        except Exception as exc:  # noqa: BLE001
            _log("control FAILED: %s (%s): %s: %s"
                 % (name, kind, type(exc).__name__, exc))
            _log(traceback.format_exc())
            return None

    def _build_window(self):
        _log("--- building panel ---")
        toolkit = self._new("Toolkit")
        container = self._new("UnoControlContainer")
        container.setModel(self._new("UnoControlContainerModel"))
        container.createPeer(toolkit, self.parent_window)
        self._root = container

        self._control(container, "lblTitle", "UnoControlFixedText",
                      "UnoControlFixedTextModel",
                      Label="Cowork — ask about this document",
                      MultiLine=False, Align=0)

        self._control(container, "txtTranscript", "UnoControlEdit",
                      "UnoControlEditModel", MultiLine=True, ReadOnly=True,
                      VScroll=True, AutoVScroll=True)

        edit = self._control(container, "txtComposer", "UnoControlEdit",
                             "UnoControlEditModel", MultiLine=False,
                             VScroll=False)
        if edit is not None:
            text_listener = _SendListener(self)
            edit.addTextListener(text_listener)
            self._listeners.append(text_listener)

        send = self._control(container, "btnSend", "UnoControlButton",
                             "UnoControlButtonModel", Label="Send", PushButtonType=0)
        clear = self._control(container, "btnClear", "UnoControlButton",
                              "UnoControlButtonModel", Label="Clear", PushButtonType=0)
        action_listener = _SendListener(self)
        for button in (send, clear):
            if button is not None:
                button.addActionListener(action_listener)
        self._listeners.append(action_listener)

        _log("controls built: %s" % sorted(self._controls))
        self._refresh_transcript()
        self._resize_listener = _RelayoutListener(self)
        for target in (self.parent_window, container):
            try:
                target.addWindowListener(self._resize_listener)
            except Exception:
                traceback.print_exc()

        self.layout()
        try:
            container.setVisible(True)
        except Exception:
            traceback.print_exc()
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
            _log("layout: parent=%dx%d container=%dx%d"
                 % (psize.Width, psize.Height, csize.Width, csize.Height))
            if width <= 0:
                width = _FALLBACK_WIDTH
            if height <= 40:
                height = 260
            inner = max(width - 2 * _MARGIN, _MIN_INNER_WIDTH)

            # Vertical stack: title / transcript (flex) / composer / buttons.
            title_h = 18
            compose_h = 26
            button_h = 26
            transcript_y = _MARGIN + title_h + 4
            buttons_y = height - _MARGIN - button_h
            composer_y = buttons_y - 4 - compose_h
            transcript_h = max(composer_y - 4 - transcript_y, 40)

            self._place("lblTitle", _MARGIN, _MARGIN, inner, title_h)
            self._place("txtTranscript", _MARGIN, transcript_y, inner, transcript_h)
            self._place("txtComposer", _MARGIN, composer_y, inner, compose_h)
            half = max((inner - 4) // 2, 30)
            self._place("btnSend", _MARGIN, buttons_y, half, button_h)
            self._place("btnClear", _MARGIN + half + 4, buttons_y,
                        inner - half - 4, button_h)
        except Exception:
            traceback.print_exc()

    def _place(self, name, x, y, w, h):
        control = self._controls.get(name)
        if control is not None:
            try:
                control.setPosSize(x, y, max(w, 10), max(h, 10), POSSIZE)
            except Exception:
                traceback.print_exc()

    # -- behaviour ----------------------------------------------------- //

    def _refresh_transcript(self):
        control = self._controls.get("txtTranscript")
        if control is None:
            return
        lines = _TRANSCRIPTS.get(_doc_key(self.frame), [])
        try:
            control.setText("\n".join(lines))
        except Exception:
            traceback.print_exc()

    def submit(self):
        """Take the composer text, append it to the transcript, and reply.

        The reply is produced by ``_respond``, which is where the agent call
        goes. For this spike it is a deterministic local stub so the panel can
        be verified without a running harness.
        """
        composer = self._controls.get("txtComposer")
        if composer is None:
            return
        try:
            text = composer.getText().strip()
        except Exception:
            traceback.print_exc()
            return
        if not text:
            return
        try:
            composer.setText("")
        except Exception:
            traceback.print_exc()

        transcript = _TRANSCRIPTS.setdefault(_doc_key(self.frame), [])
        transcript.append("You: %s" % text)
        transcript.append("Cowork: %s" % self._respond(text))
        # Keep the store bounded so a long session cannot grow without limit.
        if len(transcript) > 200:
            del transcript[:-200]
        self._refresh_transcript()

    def _respond(self, prompt):
        """The seam where the harness is called.

        Returns a fact about the live document, proving the panel can reach the
        document model through the same process.
        """
        try:
            controller = self.frame.getController()
            model = controller.getModel()
            name = model.getURL().rsplit("/", 1)[-1] or "this document"
            selection = ""
            try:
                sel = controller.getSelection()
                if sel is not None:
                    selection = " (something is selected)"
            except Exception:
                pass
            return ("[stub] I can see %s%s. Wire a harness here to go further."
                    % (name, selection))
        except Exception as exc:  # noqa: BLE001
            return "[stub] panel alive; document probe failed: %s" % exc

    def postDisposing(self):
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
            traceback.print_exc()
            return None


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    CoworkSidebarFactory, IMPL_NAME, (IMPL_NAME,))
