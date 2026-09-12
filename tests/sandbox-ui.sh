#!/usr/bin/env bash
#
# A private desktop for looking at the UI.
#
# Why this exists: the only other way found to see the panel was the GNOME
# desktop portal over D-Bus, which photographs the REAL desktop — while the
# person using the machine is working in it. That is fine once as a diagnostic
# and unacceptable as a method. This runs LibreOffice on a display nobody else
# is using, so it can be driven and captured freely.
#
# Usage:
#   tests/sandbox-ui.sh start     start the display and LibreOffice
#   tests/sandbox-ui.sh shot      capture the current screen
#   tests/sandbox-ui.sh stop      tear everything down
#   tests/sandbox-ui.sh status    report what is running
#
# Environment:
#   SANDBOX_DISPLAY   display number (default :99)
#   SANDBOX_GEOMETRY  screen size  (default 1400x1000x24)

set -uo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPLAY_NUM="${SANDBOX_DISPLAY:-:99}"
GEOMETRY="${SANDBOX_GEOMETRY:-1400x1000x24}"
SANDBOX="$WORKSPACE/research/poc/sandbox"
PROFILE="$SANDBOX/loprofile"
SHOTS="$SANDBOX/shots"
DOC="$SANDBOX/document.txt"
LOGF="$SANDBOX/panel.log"

export DISPLAY="$DISPLAY_NUM"

mkdir -p "$SHOTS"

start_display() {
  if xdpyinfo >/dev/null 2>&1; then
    echo "display $DISPLAY_NUM already up"
    return 0
  fi
  Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp >"$SANDBOX/xvfb.log" 2>&1 &
  for _ in $(seq 1 25); do
    xdpyinfo >/dev/null 2>&1 && { echo "display $DISPLAY_NUM up ($GEOMETRY)"; return 0; }
    sleep 0.4
  done
  echo "display did not come up; see $SANDBOX/xvfb.log" >&2
  return 1
}

install_extension() {
  local oxt="$WORKSPACE/ext/cowork.oxt"
  [ -f "$oxt" ] || { echo "build it first: (cd ext/oxt-proto && zip -qr ../cowork.oxt . -x '.*')" >&2; return 1; }
  mkdir -p "$PROFILE"
  rm -f "$PROFILE/.lock"
  unopkg -env:UserInstallation="file://$PROFILE" remove com.cowork.oxtproto >/dev/null 2>&1
  rm -f "$PROFILE/.lock"
  if unopkg -env:UserInstallation="file://$PROFILE" add "$oxt" 2>&1 | grep -qE "ERROR|Exception"; then
    echo "extension install failed" >&2
    return 1
  fi
  echo "extension installed into the sandbox profile"
}

start_office() {
  install_extension || return 1
  : > "$LOGF"
  # A window manager would be better; without one we set the geometry on the
  # window itself after it appears (see fit_window).
  COWORK_SIDEBAR_LOG="$LOGF" \
  COWORK_ACCEPT="socket,host=127.0.0.1,port=2099" \
  soffice --norestore \
    -env:UserInstallation="file://$PROFILE" \
    "$DOC" >"$SANDBOX/soffice.log" 2>&1 &
  for _ in $(seq 1 50); do
    if nc -z 127.0.0.1 2099 2>/dev/null || (exec 3<>/dev/tcp/127.0.0.1/2099) 2>/dev/null; then
      echo "office up, bridge acceptor ready"
      fit_window
      return 0
    fi
    sleep 0.5
  done
  echo "office did not open its acceptor; see $SANDBOX/soffice.log" >&2
  return 1
}

# Without a window manager the window keeps whatever size it asks for, which is
# usually a poor default. `xdotool` is not installed here, so this is a no-op
# unless it becomes available.
fit_window() {
  command -v xdotool >/dev/null 2>&1 || return 0
  local id
  id="$(xdotool search --class soffice 2>/dev/null | head -1)"
  [ -n "$id" ] || return 0
  xdotool windowmove "$id" 0 0
  xdotool windowsize "$id" 1200 950
}

shot() {
  local name="${1:-shot}"
  local out="$SHOTS/$name.png"
  if ! xdpyinfo >/dev/null 2>&1; then
    echo "display $DISPLAY_NUM is not running — start it first" >&2
    return 1
  fi
  xwd -root -silent | convert xwd:- "$out" || { echo "capture failed" >&2; return 1; }
  echo "$out ($(identify -format '%wx%h' "$out" 2>/dev/null))"
}

stop_all() {
  pkill -f "UserInstallation=file://$PROFILE" 2>/dev/null
  pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null
  sleep 1
  echo "sandbox stopped"
}

status() {
  xdpyinfo >/dev/null 2>&1 && echo "display   : up ($DISPLAY_NUM)" || echo "display   : down"
  (exec 3<>/dev/tcp/127.0.0.1/2099) 2>/dev/null && echo "office    : up" || echo "office    : down"
  echo "profile   : $PROFILE"
  echo "shots     : $SHOTS"
}

case "${1:-status}" in
  start)  mkdir -p "$SANDBOX"; printf 'Board Meeting Notes\n\nAttendees: Alex, Sam, Priya.\n\nDecisions\nWe agreed to move the launch to March.\n' > "$DOC"; start_display && start_office ;;
  shot)   shot "${2:-shot}" ;;
  stop)   stop_all ;;
  status) status ;;
  *)      echo "usage: $0 {start|shot [name]|stop|status}" >&2; exit 2 ;;
esac
