"""Cowork commands — a menu entry and a dispatch handler.

Why this exists beyond convenience: a sidebar panel is only reachable by opening
the sidebar and clicking it. A command can be reached from the menu, a keyboard
shortcut, or a toolbar button, and — unlike a button click — it can be invoked
programmatically, which is what makes the panel's turn path testable without a
person driving the UI.

Wiring (all three must agree, or the menu item appears and does nothing):
  * ``registry/ProtocolHandler.xcu`` — the node name MUST equal ``IMPL_NAME``
    below, and its ``Protocols`` must cover the URL prefix used here.
  * ``registry/Addons.xcu``         — the menu entry, whose ``URL`` uses that
    prefix.
  * this component                  — an ``XDispatchProvider`` reachable through
    ``com.sun.star.frame.DispatchHelper``.
"""

import os
import traceback

import uno
import unohelper

from com.sun.star.frame import XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

# MUST equal the ProtocolHandler.xcu node name.
IMPL_NAME = "com.cowork.CommandHandler"

# The URL prefix declared in ProtocolHandler.xcu.
PROTOCOL = "com.cowork"

# The UI element name registered in Sidebar.xcu / Factories.xcu.
SIDEBAR_DECK_ID = "CoworkDeck"

_LOG = os.environ.get("COWORK_SIDEBAR_LOG") or os.path.join(
    os.path.expanduser("~"), ".cache", "cowork-sidebar.log")


def _log(msg):
    try:
        with open(_LOG, "a") as fh:
            fh.write("%s\n" % msg)
    except Exception:
        pass


class _Dispatch(unohelper.Base, XDispatch):
    """One command URL, dispatched on demand."""

    def __init__(self, ctx, url, handler):
        self.ctx = ctx
        self.url = url
        # Hold the handler, not a snapshot of the frame: queryDispatch runs
        # BEFORE initialize, so at construction time the frame is not known yet.
        self.handler = handler

    def dispatch(self, url, args):
        _log("dispatch %r" % (url.Complete if url else self.url))
        try:
            self._run()
        except Exception:
            _log("dispatch failed:\n%s" % traceback.format_exc())

    def addStatusListener(self, _listener, _url):
        # The menu item is always enabled; no status broadcast is needed.
        pass

    def removeStatusListener(self, _listener, _url):
        pass

    def _run(self):
        # `initialize` is handed the Frame for the document the command was
        # invoked from, which is exactly the frame we want — no need to guess
        # from the desktop's current component.
        frame = self.handler.frame
        if frame is None:
            _log("no frame for this dispatch; ignoring")
            return
        self._show_sidebar(frame)

    def _show_sidebar(self, frame):
        """Make the Cowork deck the visible sidebar deck.

        Uses the sidebar's own API rather than poking at window geometry, so it
        works with whatever sidebar layout the user has.

        Two API details cost a round trip each and are worth recording:
        `XSidebar.showDecks()` rejects a Python sequence at the UNO bridge
        ("CannotConvertException: Type 20 is not supported"), and the deck is
        activated with `XDeck.activate()` instead.
        """
        try:
            # XFrame.getController(), not getCurrentController(): the latter
            # belongs to the *model* and fails here with AttributeError.
            controller = frame.getController()
            sidebar = controller.getSidebar()
            if sidebar is None:
                _log("this frame has no sidebar")
                return
            sidebar.setVisible(True)

            decks = sidebar.Decks
            if not decks.hasByName(SIDEBAR_DECK_ID):
                _log("the %r deck is not registered" % SIDEBAR_DECK_ID)
                return
            decks.getByName(SIDEBAR_DECK_ID).activate(True)
            _log("sidebar visible, deck %s active" % SIDEBAR_DECK_ID)
        except Exception:
            _log("could not show the sidebar:\n%s" % traceback.format_exc())


class CommandHandler(unohelper.Base, XDispatchProvider, XInitialization,
                     XServiceInfo):
    def __init__(self, ctx, *args):
        self.ctx = ctx
        self.frame = None
        self._dispatches = {}

    # XInitialization — the only lifecycle hook the office calls on us. The
    # argument is the Frame for the invoking document.
    def initialize(self, args):
        if args:
            self.frame = args[0]
        _log("command handler initialised (frame=%s)"
             % ("yes" if self.frame is not None else "no"))

    # XDispatchProvider
    def queryDispatch(self, url, _target, _flags):
        if url is None or not url.Complete.startswith(PROTOCOL + ":"):
            return None
        dispatch = self._dispatches.get(url.Complete)
        if dispatch is None:
            dispatch = _Dispatch(self.ctx, url.Complete, self)
            self._dispatches[url.Complete] = dispatch
        return dispatch

    def queryDispatches(self, requests):
        return tuple(self.queryDispatch(r.FeatureURL, r.TargetFrameName, 0)
                     for r in requests)

    # XServiceInfo
    def getImplementationName(self):
        return IMPL_NAME

    def supportsService(self, name):
        return name == IMPL_NAME

    def getSupportedServiceNames(self):
        return (IMPL_NAME,)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    CommandHandler, IMPL_NAME, (IMPL_NAME,))
