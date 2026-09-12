"""Client for the `cowork-agent` service.

The panel runs inside LibreOffice's embedded Python, which cannot spawn
processes, so it talks to a small local service instead. That service owns the
harness runtime and one conversation per document.

This module deliberately has no UNO imports: it is plain Python so it can be
tested against a live service without a GUI, which is the only way to be sure
the streaming path works before it is wired into a control.

Protocol: one JSON object per line on a loopback socket.
    → {"id": 1, "op": "ask", "doc": "...", "text": "..."}
    ← {"event": "started"} | {"event": "chunk", "text": "..."}
      {"event": "tool", "name": "..."} | {"event": "done", "text": "..."}
"""

import json
import os
import socket

DEFAULT_PORT = int(os.environ.get("COWORK_AGENT_PORT", "8765"))
CONNECT_TIMEOUT = float(os.environ.get("COWORK_CONNECT_TIMEOUT", "3"))
TURN_TIMEOUT = float(os.environ.get("COWORK_TURN_TIMEOUT", "900"))


class AgentUnavailable(RuntimeError):
    """The service is not reachable, or not ready.

    Carries a message written for the person reading the panel, not a log.
    """


class AgentClient:
    def __init__(self, port=None, host="127.0.0.1"):
        self.host = host
        self.port = port or DEFAULT_PORT

    # -- plumbing ------------------------------------------------------ //

    def _request(self, payload, timeout=CONNECT_TIMEOUT):
        try:
            connection = socket.create_connection(
                (self.host, self.port), timeout=timeout)
        except OSError as exc:
            raise AgentUnavailable(
                "the Cowork service is not running on port %d (%s).\n\n"
                "Start it with:\n"
                "    python3 dsh/libreoffice/cowork/cowork_agent.py"
                % (self.port, exc))
        connection.settimeout(timeout)
        return connection

    def ping(self):
        """Cheap liveness and readiness probe. Never raises for 'not running'."""
        try:
            connection = self._request({"id": 1, "op": "ping"})
        except AgentUnavailable:
            return {"ok": False, "reachable": False, "runtime": False}
        try:
            stream = connection.makefile("rw", encoding="utf-8", newline="\n")
            stream.write(json.dumps({"id": 1, "op": "ping"}) + "\n")
            stream.flush()
            reply = json.loads(stream.readline() or "{}")
            reply["reachable"] = True
            return reply
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reachable": True, "runtime": False,
                    "error": str(exc)}
        finally:
            try:
                connection.close()
            except Exception:
                pass

    # -- the conversation ---------------------------------------------- //

    def ask(self, document, text, on_event, should_stop=None):
        """Send one message and stream the reply.

        `on_event(kind, payload)` is called on the calling thread as events
        arrive, with kind one of ``started``, ``chunk``, ``tool``, ``done``.
        `should_stop()` is polled so a closed panel stops listening rather than
        holding a turn open.

        Returns the final assistant text.
        """
        connection = self._request({"op": "ask"})
        connection.settimeout(TURN_TIMEOUT)
        stream = connection.makefile("rw", encoding="utf-8", newline="\n")
        final = None
        try:
            stream.write(json.dumps({
                "id": 1, "op": "ask", "doc": document, "text": text,
            }) + "\n")
            stream.flush()
            for line in stream:
                if should_stop is not None and should_stop():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except Exception:
                    continue
                if message.get("ok") is False:
                    raise AgentUnavailable(message.get("error")
                                           or "the agent reported a failure")
                kind = message.get("event")
                if kind == "done":
                    final = message.get("text", "")
                    on_event("done", {"text": final})
                    return final
                on_event(kind, message)
            return final or ""
        finally:
            try:
                stream.close()
                connection.close()
            except Exception:
                pass
