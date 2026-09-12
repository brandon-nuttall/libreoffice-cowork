"""Bridge Job: opens a local UNO named-pipe acceptor from INSIDE the office.

This is what ``--accept=pipe,...`` does, minus the command line, so a
LibreOffice the user opened normally becomes reachable by local agents.
No TCP, no port, no flags, and the pipe is local-only by construction.

Shape derived from swe-sanad/LibreOffice-Claude-Connector (MIT).

Two hard-won rules encoded here:
  * ``execute()`` must NEVER raise — the Jobs framework permanently deactivates
    a job whose execute() raises, silently and for the rest of the profile.
  * The acceptor must be stored in module state BEFORE the worker thread
    starts, so a terminate arriving in that window can always stop it (an
    accept() blocked in a daemon thread would otherwise keep soffice alive).
"""

import getpass
import os
import re
import threading

import unohelper

from com.sun.star.bridge import XInstanceProvider
from com.sun.star.frame import XTerminateListener
from com.sun.star.lang import XServiceInfo
from com.sun.star.task import XJob

IMPL_NAME = "com.cowork.BridgeJob"
SERVICE_NAME = IMPL_NAME
LOG = "/home/brandon/opt/libreoffice-cowork/research/oxt-proto.log"

_state = {"started": False, "acceptor": None}
_lock = threading.Lock()


def _log(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write("JOB: %s\n" % msg)
    except Exception:
        pass


def default_pipe_name():
    user = re.sub(r"[^a-z0-9-]", "-", getpass.getuser().lower()) or "user"
    return "lo-cowork-" + user


def accept_string():
    """The UNO connect-string the acceptor listens on.

    Default is a per-user NAMED PIPE: local-only by construction, no port, no
    firewall surface, and nothing to configure. ``COWORK_ACCEPT`` overrides it
    (e.g. ``socket,host=127.0.0.1,port=2005``) which is mainly useful for
    testing in sandboxes that give each process a private mount namespace, where
    a filesystem-path pipe is not mutually visible.
    """
    override = os.environ.get("COWORK_ACCEPT")
    if override:
        return override
    return "pipe,name=%s" % (os.environ.get("COWORK_PIPE") or default_pipe_name())


class _CtxProvider(unohelper.Base, XInstanceProvider):
    """Hands the office's own context to a freshly bridged client — the same
    contract ``--accept`` fulfils."""

    def __init__(self, ctx):
        self.ctx = ctx

    def getInstance(self, name):
        if name == "StarOffice.ComponentContext":
            return self.ctx
        if name == "StarOffice.ServiceManager":
            return self.ctx.ServiceManager
        return None


class _Terminator(unohelper.Base, XTerminateListener):
    """Stops the acceptor at shutdown so a blocking accept() can never keep
    soffice.bin alive."""

    def queryTermination(self, _event):
        pass

    def notifyTermination(self, _event):
        acceptor = _state.get("acceptor")
        if acceptor is not None:
            try:
                acceptor.stopAccepting()
            except Exception:
                pass

    def disposing(self, _event):
        pass


def _accept_loop(ctx, acceptor, bridges, accept_str):
    provider = _CtxProvider(ctx)
    _log("listening on %s" % accept_str)
    count = 0
    errors = 0
    while True:
        try:
            conn = acceptor.accept(accept_str)
            errors = 0
        except Exception as exc:  # noqa: BLE001
            # Distinguish transient (client vanished mid-handshake) from fatal
            # (pipe owned by another instance, or teardown).
            errors += 1
            _log("accept error %d: %s" % (errors, exc))
            if errors >= 3:
                return
            import time
            time.sleep(0.5)
            continue
        if conn is None:  # stopAccepting() during shutdown
            _log("acceptor stopped")
            return
        count += 1
        try:
            # Empty name -> a fresh anonymous bridge per connection, exactly
            # like --accept. The bridge keeps itself alive on the connection.
            bridges.createBridge("", "urp", conn, provider)
            _log("bridged client #%d" % count)
        except Exception as exc:  # noqa: BLE001
            _log("bridge failed for client #%d: %s" % (count, exc))
            try:
                conn.close()  # nobody owns it on bridge failure
            except Exception:
                pass


def start_acceptor(ctx):
    """Idempotent: one acceptor thread per office process."""
    with _lock:
        if _state["started"]:
            return True
        _state["started"] = True

    listen_on = accept_string()
    smgr = ctx.ServiceManager
    try:
        acceptor = smgr.createInstanceWithContext(
            "com.sun.star.connection.Acceptor", ctx)
        bridges = smgr.createInstanceWithContext(
            "com.sun.star.bridge.BridgeFactory", ctx)
        _state["acceptor"] = acceptor  # publish BEFORE the worker starts
    except Exception as exc:  # noqa: BLE001
        _log("setup failed: %s" % exc)
        return False

    threading.Thread(target=_accept_loop,
                     args=(ctx, acceptor, bridges, listen_on),
                     name="cowork-acceptor",
                     daemon=True).start()

    try:
        desktop = smgr.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx)
        desktop.addTerminateListener(_Terminator())
    except Exception as exc:  # noqa: BLE001
        _log("terminate-listener registration failed (non-fatal): %s" % exc)
    return True


class BridgeJob(unohelper.Base, XJob, XServiceInfo):
    def __init__(self, ctx):
        self.ctx = ctx

    def execute(self, _args):
        # A raising Job is permanently deactivated, so never raise.
        try:
            start_acceptor(self.ctx)
        except Exception as exc:  # noqa: BLE001
            _log("execute failed: %s" % exc)
        return ()

    def getImplementationName(self):
        return IMPL_NAME

    def supportsService(self, name):
        return name == SERVICE_NAME

    def getSupportedServiceNames(self):
        return (SERVICE_NAME,)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(BridgeJob, IMPL_NAME, (SERVICE_NAME,))
