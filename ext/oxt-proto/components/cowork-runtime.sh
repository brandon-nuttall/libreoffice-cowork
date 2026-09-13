#!/usr/bin/env bash
#
# Start the Cowork runtime so the sidebar has something to talk to.
#
# Why a launcher and not a service: the runtime has to be a real process (it owns
# the agent loop, the document tools, and the loopback endpoint the panel calls),
# but LibreOffice's embedded Python cannot spawn one. Rather than ask people to
# install and babysit a systemd unit — which is what the first design did, and
# which failed by running twice and colliding on its port — the panel runs this
# script when it cannot reach the endpoint.
#
# The script is:
#   * idempotent — if the endpoint already answers, it exits immediately
#   * detached    — the runtime outlives the call, and outlives the panel
#   * quiet       — it logs next to the panel log, and prints nothing on success
#
# Usage:  cowork-runtime [--stop]
#
# Environment:
#   COWORK_PORT      endpoint port (default 8765)
#   COWORK_DSH_BIN   path to dsh/lib/bin.js if it is not on PATH
#   DSH_HOME         harness home (default ~/.dsh)

set -uo pipefail

PORT="${COWORK_PORT:-8765}"
DSH_HOME_DIR="${DSH_HOME:-$HOME/.dsh}"
PROFILE="${COWORK_PROFILE:-libreoffice}"
LOG_DIR="${COWORK_LOG_DIR:-$HOME/.cache}"
LOG="$LOG_DIR/cowork-runtime.log"

mkdir -p "$LOG_DIR"
log() { printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG" 2>/dev/null || true; }

# Probe the endpoint with python rather than a bash /dev/tcp redirect. The
# latter is a bash extension, and this script is launched by the panel through
# `/bin/sh` from LibreOffice's embedded Python: it reported the port closed while
# the runtime was demonstrably answering on it, which made a successful start look
# like a failure. python3 is already a hard requirement of this project.
port_open() {
  python3 - "$PORT" <<'PROBE' >/dev/null 2>&1
import socket, sys
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()
except OSError:
    sys.exit(1)
PROBE
}

find_dsh() {
  if [ -n "${COWORK_DSH_BIN:-}" ] && [ -e "$COWORK_DSH_BIN" ]; then
    printf '%s' "$COWORK_DSH_BIN"; return 0
  fi
  if command -v dsh >/dev/null 2>&1; then command -v dsh; return 0; fi
  # npx-style installs live five levels down; a shallower search silently misses
  # them, which is how the installer once reported a working dsh as missing.
  local hit
  for hit in "$HOME"/.npm/_npx/*/node_modules/@deepseek-ai/dsh/lib/bin.js \
             /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js \
             /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js; do
    [ -e "$hit" ] && { printf '%s' "$hit"; return 0; }
  done
  return 1
}

stop_runtime() {
  local pid pids
  # Killing the FIFO holder closes stdin, which is the runtime's own shutdown
  # path — kinder than signalling the runtime directly.
  local holder_file="${XDG_RUNTIME_DIR:-/tmp}/cowork-$PORT/holder.pid"
  if [ -f "$holder_file" ]; then
    pid="$(cat "$holder_file" 2>/dev/null || true)"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && log "closed stdin for the runtime"
    rm -f "$holder_file"
  fi
  pids="$(pgrep -f "dsh/lib/bin.js --profile $PROFILE" 2>/dev/null || true)"
  for pid in $pids; do
    kill "$pid" 2>/dev/null && log "stopped runtime pid $pid"
  done
  echo "cowork runtime stopped"
}

if [ "${1:-}" = "--stop" ]; then stop_runtime; exit 0; fi

# Already serving? Nothing to do. This is what makes the panel's "start if
# needed" call safe to make on every failed connection.
if port_open; then
  log "endpoint already listening on $PORT"
  exit 0
fi

PROFILE_DIR="$DSH_HOME_DIR/profiles/$PROFILE"
if [ ! -f "$PROFILE_DIR/cordis.patch.yml" ]; then
  log "ERROR: no '$PROFILE' profile at $PROFILE_DIR — run setup.sh"
  exit 1
fi

BIN="$(find_dsh)" || { log "ERROR: dsh not found"; exit 1; }

log "starting runtime: node $BIN --profile $PROFILE (port $PORT)"

# The runtime must be fed a stdin that NEVER closes.
#
# The `libreoffice` profile composes the SDK application, which calls
# `exitOnStdinEnd`: stdin EOF is its shutdown signal, by design. Redirecting
# stdin from /dev/null — the obvious "detach properly" move — therefore kills the
# runtime the instant it is launched, which is exactly what happened while this
# was being built: the plugin logged "listening on 127.0.0.1:8765" and the
# process was already gone.
#
# So stdin is held open by a background `tail -f /dev/null` on a FIFO, and only
# then is the runtime started against that FIFO. The FIFO also gives us a clean
# stop: killing the holder closes the pipe, which is the runtime's own exit path.
FIFO_DIR="${XDG_RUNTIME_DIR:-/tmp}/cowork-$PORT"
mkdir -p "$FIFO_DIR"
FIFO="$FIFO_DIR/stdin"
rm -f "$FIFO"
mkfifo "$FIFO" 2>/dev/null || true

if [ -p "$FIFO" ]; then
  # Hold the write end open indefinitely.
  setsid tail -f /dev/null > "$FIFO" 2>/dev/null &
  HOLDER=$!
  echo "$HOLDER" > "$FIFO_DIR/holder.pid"
fi

if command -v setsid >/dev/null 2>&1; then
  DSH_HOME="$DSH_HOME_DIR" COWORK_PORT="$PORT" \
    setsid node "$BIN" --profile "$PROFILE" < "$FIFO" >> "$LOG" 2>&1 &
else
  DSH_HOME="$DSH_HOME_DIR" COWORK_PORT="$PORT" \
    nohup node "$BIN" --profile "$PROFILE" < "$FIFO" >> "$LOG" 2>&1 &
fi

# Wait for it to answer, so the caller knows whether it worked.
for _ in $(seq 1 60); do
  if port_open; then log "runtime ready on $PORT"; exit 0; fi
  sleep 0.5
done

log "ERROR: runtime did not open $PORT within 30s"
exit 1
