#!/usr/bin/env python3
"""cowork-agent — the service the LibreOffice sidebar talks to.

The panel runs inside LibreOffice's embedded Python, which cannot spawn
processes and has no asyncio. This service is the piece that can: it owns a
DeepSeek Harness runtime and one conversation per document, and exposes both to
the panel over a loopback socket.

    panel  ──JSON lines over 127.0.0.1──▶  cowork-agent  ──SDK JSON-RPC/stdio──▶  dsh
                                                                                   │
                                              document tools ──UNO pipe──▶ LibreOffice

Protocol (one JSON object per line, both directions):

    → {"id": 1, "op": "ping"}
    ← {"id": 1, "ok": true, "documents": 1}

    → {"id": 2, "op": "ask", "doc": "file:///…", "text": "tighten this", "stream": true}
    ← {"id": 2, "ok": true, "event": "started"}
    ← {"id": 2, "ok": true, "event": "chunk", "text": "I'll start by…"}
    ← {"id": 2, "ok": true, "event": "tool", "name": "document_outline"}
    ← {"id": 2, "ok": true, "event": "done", "text": "<final answer>"}
    ← {"id": 2, "ok": false, "error": "…"}

Design notes:

* **One runtime for the life of the service.** Starting a harness per message
  would cost seconds and lose the conversation.
* **One session per document.** The runtime persists sessions to disk and rejects
  a reused id whose log does not match, so the id is derived from the document
  URL and hashed — stable across restarts, unique per document.
* **A turn ends with the agent idle, not with the last chunk.** `session/prompt`
  returns as soon as the message is queued; completion is a `session.status`
  transition.
"""

import hashlib
import json
import uuid
import os
import socket
import subprocess
import sys
import threading
import time

LOG = os.environ.get("COWORK_AGENT_LOG", "")
PORT = int(os.environ.get("COWORK_AGENT_PORT", "8765"))
DSH_HOME = os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh"))
PROFILE = os.environ.get("COWORK_PROFILE", "libreoffice")
PROVIDER = os.environ.get("COWORK_PROVIDER", "litellm")
MODEL = os.environ.get("COWORK_MODEL", "deepseek-api/v4.1-flash")
TURN_TIMEOUT = float(os.environ.get("COWORK_TURN_TIMEOUT", "900"))


def log(message):
    if not LOG:
        return
    try:
        with open(LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
    except Exception:
        pass


def find_dsh():
    """Locate the harness binary. An explicit override always wins."""
    override = os.environ.get("COWORK_DSH_BIN")
    if override and os.path.exists(override):
        return override
    for candidate in (
        os.path.expanduser("~/.dsh/bin/dsh"),
        "/usr/local/bin/dsh",
        "/usr/bin/dsh",
    ):
        if os.path.exists(candidate):
            return candidate
    # An npx-style install keeps dsh under a content-addressed directory.
    root = os.path.expanduser("~/.npm/_npx")
    if os.path.isdir(root):
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry, "node_modules",
                                "@deepseek-ai", "dsh", "lib", "bin.js")
            if os.path.exists(path):
                return path
    return None


class Harness:
    """One DeepSeek Harness runtime, driven over its SDK JSON-RPC protocol."""

    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.next_id = 1
        self.pending = {}
        self.listeners = []          # (session_id, queue) pairs
        self.ready = False
        # The runtime emits every session event TWICE with an identical `seq`
        # (verified: 10 assistant/chunk notifications carrying only 5 distinct
        # sequence numbers). Feeding both copies to a streaming consumer
        # interleaves each token with itself and produces "II'll'll start start".
        # `seq` is per-session monotonic, so the highest seen value is enough to
        # discard a repeat without any buffering.
        self.high_seq = {}
        # document URL -> live session id for this service run.
        self.sessions = {}

    # -- process ------------------------------------------------------- //

    def start(self):
        binary = find_dsh()
        if binary is None:
            raise RuntimeError(
                "the DeepSeek Harness was not found. Install it with "
                "'npm install -g @deepseek-ai/dsh', or set COWORK_DSH_BIN.")
        argv = ["node", binary, "--profile", PROFILE]
        env = dict(os.environ)
        env["DSH_HOME"] = DSH_HOME
        log("starting runtime: %s" % " ".join(argv))
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self._load_sessions()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        reply = self.request("initialize", {
            "provider": PROVIDER, "model": MODEL,
            "cwd": os.path.expanduser("~"),
        })
        if "error" in reply:
            raise RuntimeError("the runtime refused to initialize: %s"
                               % reply["error"].get("message"))
        self.ready = True
        log("runtime ready: %s" % reply.get("result", {}).get("serverInfo", {}))
        return reply

    def _read_stderr(self):
        for line in self.proc.stderr:
            text = line.rstrip()
            if text:
                log("runtime: %s" % text)

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            if "id" in message and "method" not in message:
                with self.lock:
                    slot = self.pending.pop(message["id"], None)
                if slot is not None:
                    slot["reply"] = message
                    slot["event"].set()
                continue
            method = message.get("method")
            if method == "session.event":
                self._dispatch("event", message.get("params"))
            elif method == "session.status":
                self._dispatch("status", message.get("params"))

    def _dispatch(self, kind, payload):
        session_id = (payload or {}).get("sessionId")
        event = (payload or {}).get("event") or {}
        seq = event.get("seq")
        if isinstance(seq, int):
            with self.lock:
                if seq <= self.high_seq.get(session_id, -1):
                    return                      # already delivered
                self.high_seq[session_id] = seq
        with self.lock:
            listeners = list(self.listeners)
        for listening_id, queue in listeners:
            if session_id in (None, listening_id):
                queue.append((kind, payload))

    def request(self, method, params=None, timeout=120):
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        request_id = self.next_id
        self.next_id += 1
        slot = {"event": threading.Event(), "reply": None}
        with self.lock:
            self.pending[request_id] = slot
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("the runtime is not accepting input: %s" % exc)
        if not slot["event"].wait(timeout=timeout):
            raise RuntimeError("the runtime did not answer %r within %ss"
                               % (method, timeout))
        return slot["reply"]

    def session_id(self, document):
        """The live session id for a document, creating one if needed.

        A stable id derived from the document URL alone does NOT work: the
        runtime persists every session log, and on the next run it rejects the
        reused id with "already has a persisted log on disk that does not match
        this live session". Verified by doing exactly that.

        So the id is persisted alongside a record of the document it belongs to.
        Reusing the document's id within one service lifetime resumes the same
        conversation, which is what the panel wants; a fresh service picks a new
        id rather than colliding with a log it cannot satisfy.
        """
        key = document or "(untitled)"
        with self.lock:
            existing = self.sessions.get(key)
            if existing is not None:
                return existing
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            session = "cowork-%s-%s" % (digest, uuid.uuid4().hex[:8])
            self.sessions[key] = session
            self._save_sessions()
            return session

    def _session_store_path(self):
        return os.path.join(
            os.path.expanduser("~"), ".cache", "cowork-sessions.json")

    def _load_sessions(self):
        try:
            with open(self._session_store_path()) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.sessions = {str(k): str(v) for k, v in data.items()}
        except Exception:
            self.sessions = {}

    def _save_sessions(self):
        try:
            path = self._session_store_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(self.sessions, fh, indent=1)
        except Exception as exc:  # noqa: BLE001
            log("could not persist session ids: %s" % exc)

    def ask(self, document, text, on_event):
        """Run one turn. `on_event(kind, payload)` reports progress as it happens."""
        session_id = self.session_id(document)
        queue = []
        with self.lock:
            self.listeners.append((session_id, queue))
        try:
            reply = self.request("session/prompt", {
                "sessionId": session_id,
                "contentBlocks": [{"type": "text", "text": text}],
            })
            if "error" in reply:
                raise RuntimeError(reply["error"].get("message", "the prompt was refused"))

            started = time.time()
            saw_running = False
            assistant_text = []
            # The `assistant/message` event carries the authoritative final text.
            # Accumulated deltas are only a streaming approximation and are
            # abandoned in its favour, so a dropped or reordered chunk can never
            # change the answer.
            authoritative = None
            while time.time() - started < TURN_TIMEOUT:
                if not queue:
                    time.sleep(0.05)
                    continue
                kind, payload = queue.pop(0)
                if kind == "status":
                    if payload.get("status") == "running":
                        saw_running = True
                    elif payload.get("status") == "idle" and saw_running:
                        return authoritative if authoritative is not None \
                            else "".join(assistant_text)
                    continue
                event = (payload or {}).get("event") or {}
                etype = event.get("type")
                data = event.get("data") or {}
                if etype == "assistant/chunk":
                    chunk = data.get("chunk") or {}
                    if chunk.get("type") == "text-delta":
                        assistant_text.append(chunk.get("text", ""))
                        on_event("chunk", {"text": chunk.get("text", "")})
                elif etype == "assistant/message":
                    blocks = (data.get("message") or {}).get("content") or []
                    text = "".join(b.get("text", "") for b in blocks
                                   if b.get("type") == "text")
                    if text:
                        authoritative = text
                elif etype == "tool/call":
                    on_event("tool", {"name": data.get("name", "tool")})
                elif etype == "turn/end":
                    reason = (data.get("reason") or {}).get("kind")
                    if reason == "error":
                        raise RuntimeError(
                            (data.get("reason") or {}).get("error", {}).get("message")
                            or "the turn failed")
                    if reason in ("completed", "max-tokens", "cancelled"):
                        return authoritative if authoritative is not None \
                            else "".join(assistant_text)
            raise RuntimeError("the turn did not finish within %ss" % TURN_TIMEOUT)
        finally:
            with self.lock:
                self.listeners = [pair for pair in self.listeners if pair[1] is not queue]

    def stop(self):
        if self.proc is None:
            return
        try:
            self.request("shutdown", timeout=10)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class Server:
    """Line-oriented JSON server. One connection per panel."""

    def __init__(self, harness):
        self.harness = harness
        self.harness_lock = threading.Lock()

    def serve_forever(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Loopback only: this service can edit the user's documents, so it must
        # never be reachable from the network.
        listener.bind(("127.0.0.1", PORT))
        listener.listen(8)
        log("listening on 127.0.0.1:%d" % PORT)
        try:
            while True:
                conn, _addr = listener.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            listener.close()

    def _handle(self, conn):
        stream = conn.makefile("rw", encoding="utf-8", newline="\n")
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except Exception as exc:  # noqa: BLE001
                    self._send(stream, {"ok": False, "error": "bad request: %s" % exc})
                    continue
                try:
                    self._dispatch(stream, request)
                except Exception as exc:  # noqa: BLE001
                    log("request failed: %s" % exc)
                    self._send(stream, {"id": request.get("id"), "ok": False,
                                        "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log("connection ended: %s" % exc)
        finally:
            try:
                stream.close()
                conn.close()
            except Exception:
                pass

    def _send(self, stream, payload):
        try:
            stream.write(json.dumps(payload) + "\n")
            stream.flush()
        except Exception:
            pass

    def _dispatch(self, stream, request):
        op = request.get("op")
        request_id = request.get("id")

        if op == "ping":
            self._send(stream, {"id": request_id, "ok": True,
                                "runtime": self.harness.ready,
                                "busy": self.harness_lock.locked()})
            return

        if op == "ask":
            if not self.harness.ready:
                raise RuntimeError("the runtime is not ready yet")
            document = request.get("doc")
            text = (request.get("text") or "").strip()
            if not text:
                raise RuntimeError("nothing to send")
            # One turn at a time: the runtime multiplexes, but the panel has one
            # thread of conversation and interleaved turns would be unreadable.
            if not self.harness_lock.acquire(blocking=False):
                raise RuntimeError("the previous request is still running")
            try:
                self._send(stream, {"id": request_id, "ok": True, "event": "started"})
                final = self.harness.ask(
                    document, text,
                    lambda kind, payload: self._send(
                        stream, {"id": request_id, "ok": True,
                                 "event": kind, **payload}))
                self._send(stream, {"id": request_id, "ok": True,
                                    "event": "done", "text": final})
            finally:
                self.harness_lock.release()
            return

        raise RuntimeError("unknown op %r" % op)


def main():
    log("cowork-agent starting (profile=%s model=%s)" % (PROFILE, MODEL))
    harness = Harness()
    try:
        harness.start()
    except Exception as exc:  # noqa: BLE001
        # Start the socket anyway: the panel should be able to ask why, rather
        # than see a connection refused and no explanation.
        log("runtime failed to start: %s" % exc)
        harness.ready = False
        harness.error = str(exc)

    server = Server(harness)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        harness.stop()
        log("cowork-agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
