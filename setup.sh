#!/usr/bin/env bash
#
# LibreOffice Cowork — installer
#
#   curl -fsSL https://raw.githubusercontent.com/brandon-nuttall/libreoffice-cowork/main/setup.sh | bash
#
# Installs two things:
#   1. the Cowork extension (.oxt) into your LibreOffice
#   2. the `libreoffice` DSH profile into your DeepSeek Harness home
#
# Both are needed. The extension is what lets the agent see the document you
# have open; the profile is what gives the agent document tools and an office
# persona. Installing one without the other does nothing useful.
#
# Idempotent: safe to re-run, which is also how you upgrade.

set -euo pipefail

REPO="${COWORK_REPO:-brandon-nuttall/libreoffice-cowork}"
BRANCH="${COWORK_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

DSH_HOME_DIR="${DSH_HOME:-$HOME/.dsh}"
PROFILE_NAME="libreoffice"
ASSUME_YES=0
SKIP_VERIFY=0

# ── output ──────────────────────────────────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=''; DIM=''; G=''; Y=''; R=''; N=''
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%sError:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
LibreOffice Cowork installer

Usage: setup.sh [options]

Options:
  -y, --yes           do not prompt before writing files
      --skip-verify   install without testing the result
      --uninstall     remove the extension and the DSH profile
  -h, --help          show this help

Environment:
  DSH_HOME            harness home to install the profile into (default ~/.dsh)
  COWORK_REPO         owner/repo to fetch from (default ${REPO})
  COWORK_BRANCH       branch to fetch from (default ${BRANCH})
  COWORK_LO_PROFILE   LibreOffice user profile to install into (default: the
                      normal per-user profile)
EOF
}

for arg in "$@"; do
  case "$arg" in
    -y|--yes)     ASSUME_YES=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --uninstall)  UNINSTALL=1 ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "unknown option: $arg (try --help)" ;;
  esac
done

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  read -r reply </dev/tty || return 1
  case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# ── locate a source tree ────────────────────────────────────────────────────
# Works two ways: from a local checkout (run `./setup.sh`), or piped from curl,
# in which case the tree is downloaded into a temporary directory.

SRC=""
TMPDIR_CREATED=""
cleanup() { [ -n "$TMPDIR_CREATED" ] && rm -rf "$TMPDIR_CREATED"; }
trap cleanup EXIT

resolve_source() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo '')"
  if [ -n "$here" ] && [ -f "$here/dsh/libreoffice/cowork/cowork-office.mjs" ]; then
    SRC="$here"
    ok "using the local checkout at $SRC"
    return
  fi
  TMPDIR_CREATED="$(mktemp -d)"
  SRC="$TMPDIR_CREATED"
  command -v curl >/dev/null || die "curl is required to download the source"
  step "Downloading LibreOffice Cowork"
  local url="https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}"
  curl -fsSL "$url" | tar xz -C "$SRC" --strip-components=1 \
    || die "could not download $url — check COWORK_REPO/COWORK_BRANCH"
  [ -f "$SRC/dsh/libreoffice/cowork/cowork-office.mjs" ] \
    || die "the downloaded archive did not look like this project"
  ok "downloaded to $SRC"
}

# ── checks ──────────────────────────────────────────────────────────────────

SOFFICE=""
UNOPKG=""

# `unopkg` and LibreOffice must agree on which user profile they act on. An empty
# value means the normal per-user default; COWORK_LO_PROFILE overrides it, which
# is mainly useful for testing and for a deployment with a non-standard profile.
LO_ARGS=()
if [ -n "${COWORK_LO_PROFILE:-}" ]; then
  case "$COWORK_LO_PROFILE" in
    file://*) LO_ARGS+=("-env:UserInstallation=${COWORK_LO_PROFILE}") ;;
    *)        LO_ARGS+=("-env:UserInstallation=file://${COWORK_LO_PROFILE}") ;;
  esac
fi

find_libreoffice() {
  step "Checking LibreOffice"
  SOFFICE="$(command -v soffice || command -v libreoffice || true)"
  [ -n "$SOFFICE" ] || die "LibreOffice was not found on PATH.
  Install it first, e.g.  sudo apt install libreoffice"

  UNOPKG="$(command -v unopkg || true)"
  if [ -z "$UNOPKG" ] && [ -x "$(dirname "$(readlink -f "$SOFFICE")")/unopkg" ]; then
    UNOPKG="$(dirname "$(readlink -f "$SOFFICE")")/unopkg"
  fi
  [ -n "$UNOPKG" ] || die "unopkg was not found; it ships with LibreOffice.
  On Debian/Ubuntu:  sudo apt install libreoffice-common"

  local version
  version="$("$SOFFICE" --version 2>/dev/null | head -1 || echo 'unknown')"
  ok "$version"
  ok "unopkg at $UNOPKG"
}

PYTHON=""
find_python_uno() {
  step "Checking the Python UNO bridge"
  local candidates=()
  # LibreOffice's own bundled interpreter always has uno and is the safest
  # choice; the system python3 works if python3-uno is installed.
  local prog_dir
  prog_dir="$(dirname "$(readlink -f "$SOFFICE")")"
  [ -x "$prog_dir/python" ] && candidates+=("$prog_dir/python")
  command -v python3 >/dev/null && candidates+=("python3")

  for candidate in "${candidates[@]}"; do
    if "$candidate" -c 'import uno' >/dev/null 2>&1; then
      PYTHON="$candidate"
      ok "$candidate can import uno"
      return
    fi
  done

  # No usable interpreter. Distro package names differ, so try to install it.
  warn "no Python interpreter with the 'uno' module was found"
  say ""
  say "  The bridge needs it. On Debian/Ubuntu:"
  say "      sudo apt install python3-uno"
  say "  On Fedora:      sudo dnf install libreoffice-pyuno"
  say "  On Arch:        sudo pacman -S libreoffice-fresh  (uno is included)"
  say ""

  if command -v apt-get >/dev/null && command -v sudo >/dev/null; then
    if confirm "Install python3-uno with apt now?"; then
      sudo apt-get install -y python3-uno || die "apt-get install python3-uno failed"
      if python3 -c 'import uno' >/dev/null 2>&1; then
        PYTHON="python3"; ok "python3-uno installed"
        return
      fi
      die "python3-uno was installed but 'import uno' still fails"
    fi
  fi
  die "install a Python UNO binding and re-run"
}

DSH_BIN=""
find_dsh() {
  step "Checking the DeepSeek Harness"
  if [ -n "${COWORK_DSH_BIN:-}" ]; then
    DSH_BIN="$COWORK_DSH_BIN"
  elif command -v dsh >/dev/null; then
    DSH_BIN="$(command -v dsh)"
  else
    # An npx-style install keeps dsh under a content-addressed directory.
    local found
    found="$(find "$HOME/.npm/_npx" -maxdepth 4 -type f \
              -path '*/@deepseek-ai/dsh/lib/bin.js' 2>/dev/null | head -1 || true)"
    [ -n "$found" ] && DSH_BIN="$found"
  fi

  if [ -z "$DSH_BIN" ] || [ ! -e "$DSH_BIN" ]; then
    die "the DeepSeek Harness ('dsh') was not found.
  Install it with:   npm install -g @deepseek-ai/dsh
  or point at it:    COWORK_DSH_BIN=/path/to/dsh/lib/bin.js ./setup.sh"
  fi
  ok "$DSH_BIN"
}

# The profile composes bundles that ship inside the dsh installation, so the
# installed profile needs a node_modules that can resolve them.
DSH_INSTALL_ROOT=""
find_dsh_install_root() {
  local bin="$DSH_BIN"
  local dir
  dir="$(cd "$(dirname "$bin")/.." && pwd)"      # .../@deepseek-ai/dsh
  local root
  root="$(cd "$dir/../.." && pwd)"               # .../node_modules
  if [ -d "$root/@deepseek-ai/dsh-base" ]; then
    DSH_INSTALL_ROOT="$root"
    return
  fi
  # Fall back to wherever dsh-base actually lives.
  dir="$(dirname "$(find "$root" -maxdepth 3 -type d -name dsh-base 2>/dev/null | head -1)")"
  [ -d "$dir/dsh-base" ] && DSH_INSTALL_ROOT="$dir"
}

# ── install ─────────────────────────────────────────────────────────────────

install_agent_service() {
  step "Installing the Cowork agent service"
  # The panel runs inside LibreOffice's embedded Python, which cannot spawn
  # processes, so the agent that owns the harness runtime is a separate service.
  # Without it the panel can render but cannot answer.
  local share="$HOME/.local/share/libreoffice-cowork"
  mkdir -p "$share"
  install -m 755 "$SRC/dsh/libreoffice/cowork/cowork_agent.py" "$share/cowork_agent.py"
  install -m 644 "$SRC/dsh/libreoffice/cowork/uno_bridge.py" "$share/uno_bridge.py" 2>/dev/null || true
  # The agent resolves the helper beside itself, but the profile copy is the one
  # the document tools use; keep both in step by pointing at the installed profile.
  ok "installed to $share"

  if [ -z "${DSH_BIN:-}" ]; then
    warn "no dsh binary known; the service will search for one at start"
  fi

  if command -v systemctl >/dev/null && [ -d "$HOME/.config/systemd/user" -o -w "$HOME/.config" ]; then
    local unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    sed "s|ExecStart=%h/.local/share/libreoffice-cowork/cowork_agent.py|ExecStart=$share/cowork_agent.py|" \
      "$SRC/dist/cowork-agent.service" > "$unit_dir/cowork-agent.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    if systemctl --user enable --now cowork-agent.service >/dev/null 2>&1; then
      ok "systemd user service 'cowork-agent' enabled and started"
      return
    fi
    warn "could not enable the systemd user service"
  else
    warn "no systemd user session here, so the agent will not start automatically"
  fi

  # Start it in the background anyway: a panel with no agent behind it is a
  # dead panel, and that is a bad first impression.
  if python3 "$share/cowork_agent.py" >/dev/null 2>&1 &
  then
    sleep 2
    ok "agent started in the background (pid $!)"
    say "      It will not survive a reboot; start it again, or use systemd."
  else
    say "      Start it with:  python3 $share/cowork_agent.py"
  fi
}

build_oxt() {
  step "Building the extension"
  command -v zip >/dev/null || die "the 'zip' command is required to build the .oxt"
  local out="$SRC/ext/cowork.oxt"
  rm -f "$out"
  ( cd "$SRC/ext/oxt-proto" && zip -qr "$out" . -x '.*' )
  [ -f "$out" ] || die "building the .oxt failed"
  ok "$(du -h "$out" | cut -f1) extension built"
}

install_oxt() {
  step "Installing the extension into LibreOffice"

  # `unopkg` refuses to start while a .lock exists, and a killed or internally
  # failed run leaves one behind — after which every later invocation reports
  # "the lock file indicates it is already running" even though nothing is.
  # When an explicit profile is in use we own it, so a stale lock is ours to
  # clear; a lock only survives if a real unopkg is mid-run.
  unopkg_retry() {
    local attempt out
    for attempt in 1 2 3; do
      out="$("$UNOPKG" "${LO_ARGS[@]:-}" "$@" 2>&1)" && { printf '%s' "$out"; return 0; }
      case "$out" in
        *"lock file"*)
          if [ -n "${COWORK_LO_PROFILE:-}" ] && ! pgrep -x unopkg >/dev/null 2>&1; then
            rm -f "${COWORK_LO_PROFILE#file://}/.lock"
            sleep 1
            continue
          fi
          ;;
      esac
      printf '%s' "$out"
      return 1
    done
    printf '%s' "$out"
    return 1
  }

  # Remove first so re-running the installer is an upgrade, not a duplicate error.
  unopkg_retry remove com.cowork.oxtproto >/dev/null 2>&1 || true

  if ! log="$(unopkg_retry add "$SRC/ext/cowork.oxt")"; then
    say "$log" | grep -v -E 'dconf|^\s*$' >&2 || true
    die "unopkg could not install the extension (see above)."
  fi

  if unopkg_retry list 2>/dev/null | grep -q 'com.cowork.oxtproto'; then
    ok "extension registered"
  else
    die "the extension was added but does not appear in 'unopkg list'"
  fi
}

install_profile() {
  step "Installing the DSH profile into $DSH_HOME_DIR"
  local dest="$DSH_HOME_DIR/profiles/$PROFILE_NAME"
  mkdir -p "$DSH_HOME_DIR/profiles"
  rm -rf "$dest"
  cp -r "$SRC/dsh/libreoffice" "$dest"

  find_dsh_install_root
  if [ -n "$DSH_INSTALL_ROOT" ]; then
    ln -sfn "$DSH_INSTALL_ROOT" "$dest/node_modules"
    ok "profile installed, bundles resolved from $DSH_INSTALL_ROOT"
  else
    warn "could not locate the dsh installation's node_modules"
    warn "the profile may fail to boot; set it up manually if so:"
    warn "  ln -s <dsh-install>/node_modules $dest/node_modules"
  fi
  ok "documents tools + office persona + 4 skills"
}

# ── verify ──────────────────────────────────────────────────────────────────

verify() {
  [ "$SKIP_VERIFY" = 1 ] && return 0
  step "Verifying"

  # The profile must compose. --dump-config builds the whole plugin tree and
  # exits, which catches a broken row without starting a runtime.
  local dump
  if dump="$(DSH_HOME="$DSH_HOME_DIR" node "$DSH_BIN" --profile "$PROFILE_NAME" --dump-config 2>&1)"; then
    if printf '%s' "$dump" | grep -q 'cowork-office'; then
      ok "the '$PROFILE_NAME' profile composes"
    else
      warn "the profile composed but the document tools are missing from it"
    fi
  else
    printf '%s\n' "$dump" | tail -20 >&2
    die "the '$PROFILE_NAME' profile failed to compose (see above)"
  fi

  # Does the agent service answer? This is the link the panel depends on, and
  # the one most likely to be missing.
  if command -v systemctl >/dev/null && systemctl --user is-active cowork-agent.service >/dev/null 2>&1; then
    ok "the cowork-agent service is running"
  else
    local reachable
    reachable="$(python3 - <<'PROBE' 2>/dev/null || true
import socket
try:
    socket.create_connection(("127.0.0.1", 8765), timeout=2).close()
    print("yes")
except Exception:
    print("no")
PROBE
)"
    if [ "$reachable" = "yes" ]; then
      ok "the cowork-agent service is answering on port 8765"
    else
      warn "the cowork-agent service is not running yet"
      say "      The sidebar will tell you the same thing, and how to start it."
    fi
  fi

  say ""
  say "  ${DIM}The extension starts its bridge when LibreOffice starts, so a running${N}"
  say "  ${DIM}instance needs a restart before an agent can reach it.${N}"
}

# ── uninstall ───────────────────────────────────────────────────────────────

do_uninstall() {
  step "Removing LibreOffice Cowork"
  local unopkg_bin
  unopkg_bin="$(command -v unopkg || true)"
  if [ -n "$unopkg_bin" ]; then
    "$unopkg_bin" "${LO_ARGS[@]:-}" remove com.cowork.oxtproto >/dev/null 2>&1 \
      && ok "extension removed" || warn "the extension was not installed"
  fi
  local dest="$DSH_HOME_DIR/profiles/$PROFILE_NAME"
  if [ -e "$dest" ]; then
    rm -rf "$dest"; ok "profile removed from $DSH_HOME_DIR"
  else
    warn "no profile at $dest"
  fi
  if command -v systemctl >/dev/null; then
    systemctl --user disable --now cowork-agent.service >/dev/null 2>&1 \
      && ok "agent service stopped and disabled" || true
    rm -f "$HOME/.config/systemd/user/cowork-agent.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  rm -rf "$HOME/.local/share/libreoffice-cowork"
  say ""
  say "Restart LibreOffice to unload the extension."
}

# ── main ────────────────────────────────────────────────────────────────────

say "${B}LibreOffice Cowork${N} — an agent that works on the document you have open"

if [ "${UNINSTALL:-0}" = 1 ]; then
  do_uninstall
  exit 0
fi

find_libreoffice
find_python_uno
find_dsh
resolve_source

if [ "$ASSUME_YES" = 0 ]; then
  say ""
  say "About to install:"
  say "  extension  -> your LibreOffice"
  say "  profile    -> $DSH_HOME_DIR/profiles/$PROFILE_NAME"
  confirm "Continue?" || { say "Cancelled."; exit 0; }
fi

build_oxt
install_oxt
install_profile
install_agent_service
verify

say ""
say "${G}${B}Done.${N}"
say ""
say "Next:"
say "  1. Start (or restart) LibreOffice and open a document."
say "  2. Open the Cowork deck in the sidebar — ${B}View ▸ Sidebar ▸ Cowork${N}."
say ""
say "  The extension's bridge starts with LibreOffice. The ${B}cowork-agent${N} service"
say "  must also be running — it is what actually reaches the model. If the"
say "  sidebar says it cannot reach the service, start it with:"
say "      python3 $HOME/.local/share/libreoffice-cowork/cowork_agent.py"
say ""
say "${DIM}Uninstall any time with:  setup.sh --uninstall${N}"
