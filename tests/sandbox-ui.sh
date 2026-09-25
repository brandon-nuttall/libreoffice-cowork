#!/usr/bin/env bash
#
# A private desktop for looking at the UI.
#
# TWO FAILURES THIS SCRIPT EXISTS TO PREVENT
# ------------------------------------------
# 1. WINDOWS ON THE USER'S DESKTOP. LibreOffice is single-instance per user
#    profile. Launch it with a profile that does not exist yet and it falls back
#    to the default profile, finds the user's running office, and HANDS THE
#    DOCUMENT OFF to it -- popping a window onto the user's desktop. Reported as
#    "you are popping it on my desktop not your private display". Fixed by
#    creating the profile FIRST, so there is nothing to fall back FROM.
#
# 2. NO WINDOW AT ALL. On a fresh profile LibreOffice shows its first-run
#    "Welcome" wizard, and the document window is never mapped. The process runs,
#    the bridge answers, and the screen stays empty -- which is what made this
#    look like a missing window manager for far too long. Fixed by pre-seeding
#    registrymodifications.xcu with FirstRun=false.
#
# Verified on Xvfb, no window manager needed: both `SAL_USE_VCLPLUGIN=gen` and
# `gtk3` map windows, and gtk3 is used because it draws the real UI.
#
# So every launch here:
#   1. creates and pre-seeds the profile, so there is no fallback and no wizard
#   2. forces DISPLAY and unsets WAYLAND_DISPLAY so it cannot pick the real session
#   3. verifies a window appeared on the private display, and reports if not
#   4. counts windows on :0 before and after, and kills the office if the user's
#      desktop gained one
#
# Usage:
#   tests/sandbox-ui.sh start     start the display and LibreOffice
#   tests/sandbox-ui.sh shot      capture the current screen
#   tests/sandbox-ui.sh stop      tear everything down
#   tests/sandbox-ui.sh status    report what is running

set -uo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPLAY_NUM="${SANDBOX_DISPLAY:-:99}"
GEOMETRY="${SANDBOX_GEOMETRY:-1400x1000x24}"
SANDBOX="$WORKSPACE/research/poc/sandbox"
PROFILE="$SANDBOX/loprofile"
SHOTS="$SANDBOX/shots"
DOC="$SANDBOX/document.txt"
PANEL_LOG="$SANDBOX/panel.log"
PORT="${SANDBOX_PORT:-2098}"
# The runtime's HTTP port. The sandbox MUST NOT use the default 8765: a sandbox
# runtime left on the user's port gets adopted by the user's panel, and the
# agent then tries to act on a document in a sandbox office that is not there
# ("I can't reach LibreOffice yet ... something else holds port 8765").
RUNTIME_PORT="${SANDBOX_RUNTIME_PORT:-8799}"

mkdir -p "$SHOTS" "$PROFILE"

start_display() {
  if DISPLAY="$DISPLAY_NUM" xdpyinfo >/dev/null 2>&1; then
    echo "display $DISPLAY_NUM already up"; return 0
  fi
  Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp >"$SANDBOX/xvfb.log" 2>&1 &
  for _ in $(seq 1 25); do
    DISPLAY="$DISPLAY_NUM" xdpyinfo >/dev/null 2>&1 && { echo "display $DISPLAY_NUM up ($GEOMETRY)"; return 0; }
    sleep 0.4
  done
  echo "display did not come up; see $SANDBOX/xvfb.log" >&2; return 1
}

window_count() { DISPLAY="$1" xwininfo -root -children 2>/dev/null | grep -c '^     0x' || true; }

install_extension() {
  local oxt="$WORKSPACE/ext/cowork.oxt"
  [ -f "$oxt" ] || { echo "build the .oxt first" >&2; return 1; }
  mkdir -p "$PROFILE/user"
  # Pre-seed so the first-run wizard never appears. Without this the document
  # window is never mapped and the screen stays blank.
  if [ ! -f "$PROFILE/user/registrymodifications.xcu" ]; then
    cat > "$PROFILE/user/registrymodifications.xcu" <<'XCU'
<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>
</oor:items>
XCU
  fi; rm -f "$PROFILE/.lock"
  unopkg -env:UserInstallation="file://$PROFILE" remove com.cowork.oxtproto >/dev/null 2>&1
  rm -f "$PROFILE/.lock"
  if unopkg -env:UserInstallation="file://$PROFILE" add "$oxt" 2>&1 | grep -qE "ERROR|Exception"; then
    echo "extension install FAILED -- is LibreOffice running? it holds the cache" >&2
    return 1
  fi
  echo "extension installed into the sandbox profile"
}

start_office() {
  install_extension || return 1

  local before_user after_user
  before_user="$(window_count :0)"

  : > "$PANEL_LOG"
  # The profile is created above and passed explicitly. This is the whole fix:
  # with the profile already present there is nothing for LibreOffice to fall
  # back from, so it never finds the user's instance to hand off to.
  rm -f "$DOC"'.~lock.'"$(basename "$DOC")" 2>/dev/null || true
  env -u WAYLAND_DISPLAY \
      DISPLAY="$DISPLAY_NUM" XDG_SESSION_TYPE=x11 \
      SAL_USE_VCLPLUGIN="${SANDBOX_VCL:-gtk3}" GDK_BACKEND=x11 \
      COWORK_SIDEBAR_LOG="$PANEL_LOG" \
      COWORK_ACCEPT="socket,host=127.0.0.1,port=$PORT" \
      COWORK_PORT="$RUNTIME_PORT" \
      soffice --norestore -env:UserInstallation="file://$PROFILE" "$DOC" \
      >"$SANDBOX/soffice.log" 2>&1 &
  local pid=$!

  local ready=no
  for _ in $(seq 1 60); do
    if (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then ready=yes; break; fi
    sleep 0.5
  done
  if [ "$ready" != yes ]; then
    echo "office never opened its acceptor; killing pid $pid" >&2
    kill "$pid" 2>/dev/null; return 1
  fi

  local windows=0
  for _ in $(seq 1 24); do
    windows="$(window_count "$DISPLAY_NUM")"
    [ "$windows" -gt 0 ] && break
    sleep 0.5
  done
  echo "bridge ready on port $PORT"
  if [ "$windows" -gt 0 ]; then
    echo "window on $DISPLAY_NUM: yes ($windows)"
  else
    echo "window on $DISPLAY_NUM: NO -- the office is up and reachable but nothing" >&2
    echo "  is mapped. A window manager is the usual cause:" >&2
    echo "    sudo apt install openbox   then re-run start" >&2
  fi

  after_user="$(window_count :0)"
  if [ "${after_user:-0}" -gt "${before_user:-0}" ]; then
    echo "!! a window appeared on YOUR display -- killing the sandbox office" >&2
    kill "$pid" 2>/dev/null; return 1
  fi
  echo "no new windows on :0 (your desktop is untouched)"
}

shot() {
  local name="${1:-shot}" out="$SHOTS/$name.png"
  DISPLAY="$DISPLAY_NUM" xdpyinfo >/dev/null 2>&1 || { echo "display down" >&2; return 1; }
  DISPLAY="$DISPLAY_NUM" xwd -root -silent | convert xwd:- "$out" || return 1
  echo "$out"
}

stop_all() {
  pkill -f "UserInstallation=file://$PROFILE" 2>/dev/null
  pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null
  sleep 1; echo "sandbox stopped"
}

status() {
  DISPLAY="$DISPLAY_NUM" xdpyinfo >/dev/null 2>&1 \
    && echo "display : $DISPLAY_NUM up, $(window_count "$DISPLAY_NUM") window(s)" \
    || echo "display : down"
  (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null && echo "office  : up (port $PORT)" || echo "office  : down"
  for pid in $(pgrep soffice.bin 2>/dev/null); do
    local d; d="$(tr '\0' '\n' < /proc/"$pid"/environ 2>/dev/null | grep '^DISPLAY=' | cut -d= -f2)"
    echo "  soffice pid $pid on DISPLAY=${d:-?}"
  done
}

case "${1:-status}" in
  start)
    printf 'Board Meeting Notes\n\nAttendees: Alex, Sam, Priya.\n\nDecisions\nWe agreed to move the launch to March.\n' > "$DOC"
    start_display && start_office ;;
  shot)   shot "${2:-shot}" ;;
  stop)   stop_all ;;
  status) status ;;
  *)      echo "usage: $0 {start|shot [name]|stop|status}" >&2; exit 2 ;;
esac
