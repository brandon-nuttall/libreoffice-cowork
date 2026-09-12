"""Minimal UNO component: proves a .oxt-bundled Python service can be
instantiated from the office process and host the bridge listener."""
import threading

import uno
import unohelper

from com.sun.star.lang import XServiceInfo
from com.sun.star.lang import XInitialization

LOG = "/home/brandon/opt/libreoffice-cowork/research/oxt-proto.log"


def _log(msg):
    with open(LOG, "a") as fh:
        fh.write(msg + "\n")


class CoworkBridge(unohelper.Base, XServiceInfo, XInitialization):
    IMPLE_NAME = "com.cowork.Bridge"
    SERVICE_NAMES = ("com.cowork.Bridge",)

    def __init__(self, ctx, *args):
        self.ctx = ctx
        self._server = None
        self._thread = None
        _log("component constructed; ctx=%r" % (ctx,))
        # Start eagerly from the constructor: XInitialization was not reachable
        # through createInstanceWithArgumentsAndContext in this build, and the
        # constructor is the one hook LibreOffice definitely invokes.
        try:
            _log("eager start -> %s" % self.start())
        except Exception as exc:  # noqa: BLE001
            _log("eager start raised %s: %s" % (type(exc).__name__, exc))

    # XInitialization — the ONLY lifecycle hook reachable over the bridge.
    # Plain Python methods are invisible through UNO, so the listener is
    # started here rather than from a custom start() method.
    def initialize(self, args):
        _log("initialize() called with %r" % (args,))
        try:
            _log("start() -> %s" % self.start())
        except Exception as exc:  # noqa: BLE001
            _log("start() raised %s: %s" % (type(exc).__name__, exc))

    # XServiceInfo
    def getImplementationName(self):
        return self.IMPLE_NAME

    def supportsService(self, name):
        return name in self.SERVICE_NAMES

    def getSupportedServiceNames(self):
        return self.SERVICE_NAMES

    def start(self):
        """Idempotent: bring the listener up once."""
        if self._server is not None:
            return "already running"
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                res = {"path": self.path}
                try:
                    desktop = outer.ctx.ServiceManager.createInstanceWithContext(
                        "com.sun.star.frame.Desktop", outer.ctx)
                    enum = desktop.Components.createEnumeration()
                    docs = []
                    while enum.hasMoreElements():
                        docs.append(enum.nextElement().getURL() or "(untitled)")
                    res["documents"] = docs
                except Exception as exc:  # noqa: BLE001
                    res["error"] = "%s: %s" % (type(exc).__name__, exc)
                raw = json.dumps(res).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 2097), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        _log("listener started on 2097")
        return "started"


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    CoworkBridge, "com.cowork.Bridge", ("com.cowork.Bridge",))
