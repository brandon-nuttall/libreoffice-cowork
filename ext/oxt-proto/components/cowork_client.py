"""Client for the Cowork runtime endpoint.

The runtime serves the conversation itself, on loopback, from inside the process
that owns the agent loop and the document tools. There is no separate agent
service any more: it was a second process, a second protocol, a second thing to
install, and it could run twice and collide on its port.

The panel runs in LibreOffice's embedded Python, which cannot spawn the runtime,
so when the endpoint does not answer this also starts it, via a launcher script
bundled with the extension. That keeps the user-facing story to "open the panel":
nothing to install, nothing to keep running by hand.

No UNO imports here on purpose — this is plain Python so the streaming path can be
tested against a live runtime without a GUI.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_PORT = int(os.environ.get("COWORK_PORT", "8765"))
CONNECT_TIMEOUT = float(os.environ.get("COWORK_CONNECT_TIMEOUT", "3"))
TURN_TIMEOUT = float(os.environ.get("COWORK_TURN_TIMEOUT", "900"))


class AgentUnavailable(RuntimeError):
    """The endpoint is not reachable, with a message written for the reader."""


def _port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


class AgentClient:
    def __init__(self, port=None, host="127.0.0.1"):
        self.host = host
        self.port = port or DEFAULT_PORT

    # -- lifecycle ----------------------------------------------------- //

    def ping(self):
        """Cheap readiness probe. Never raises for 'not running'."""
        try:
            with urllib.request.urlopen(
                    "http://%s:%d/ping" % (self.host, self.port), timeout=CONNECT_TIMEOUT) as r:
                body = json.loads(r.read().decode() or "{}")
                body["reachable"] = True
                return body
        except Exception:
            return {"ok": False, "reachable": False, "runtime": False}

    def ensure_running(self):
        """Start the runtime if nothing is serving, and wait for it.

        The launcher is idempotent — it exits immediately when the endpoint
        already answers — so calling this on every failed connection is safe.
        """
        if _port_open(self.port):
            return True
        launcher = os.environ.get("COWORK_LAUNCHER") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cowork-runtime.sh")
        if not os.path.exists(launcher):
            return False
        try:
            os.chmod(launcher, 0o755)
        except OSError:
            pass
        try:
            subprocess.Popen(["/bin/sh", launcher],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            return False
        for _ in range(60):
            if _port_open(self.port):
                return True
            time.sleep(0.5)
        return False

    # -- the conversation ---------------------------------------------- //

    def ask(self, document, text, on_event, should_stop=None):
        """Send one message and stream the reply.

        `on_event(kind, payload)` is called on the calling thread as events
        arrive: ``started``, ``chunk``, ``tool``, ``done``.
        """
        if not _port_open(self.port):
            # First failure starts it; a second failure is a real error.
            if not self.ensure_running():
                raise AgentUnavailable(
                    "the Cowork runtime is not running and could not be started.\n\n"
                    "Start it by hand to see why:\n"
                    "    %s" % os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "cowork-runtime.sh"))

        payload = json.dumps({"doc": document, "text": text}).encode()
        request = urllib.request.Request(
            "http://%s:%d/ask" % (self.host, self.port), data=payload,
            headers={"content-type": "application/json"})

        final = None
        try:
            response = urllib.request.urlopen(request, timeout=TURN_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            raise AgentUnavailable("the runtime refused the request: %s" % detail)
        except Exception as exc:  # noqa: BLE001
            raise AgentUnavailable("could not reach the runtime: %s" % exc)

        try:
            for raw in response:
                if should_stop is not None and should_stop():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("ok") is False:
                    raise AgentUnavailable(message.get("error") or "the agent failed")
                kind = message.get("event")
                if kind == "done":
                    final = message.get("text", "")
                    on_event("done", {"text": final})
                    return final
                on_event(kind, message)
            return final or ""
        finally:
            response.close()
