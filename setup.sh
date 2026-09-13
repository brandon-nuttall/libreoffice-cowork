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

  # Search every place a dsh actually ends up, in order of how deliberate the
  # installation was. Each of these has been the *only* hit on some machine:
  #
  #   * COWORK_DSH_BIN      explicit override
  #   * PATH                a global npm install
  #   * ~/.npm/_npx/*       `npx @deepseek-ai/dsh`, which is what this project's
  #                         own documentation produces and therefore the common case
  #   * a local node_modules, for someone running from a checkout
  #
  # The npx glob goes deep on purpose: the package lives at
  # ~/.npm/_npx/<hash>/node_modules/@deepseek-ai/dsh/lib/bin.js, which is five
  # levels down. An earlier `find -maxdepth 4` silently matched nothing, so a
  # perfectly good installation was reported as missing.
  local candidate=""
  if [ -n "${COWORK_DSH_BIN:-}" ]; then
    candidate="$COWORK_DSH_BIN"
  elif command -v dsh >/dev/null 2>&1; then
    candidate="$(command -v dsh)"
  else
    local hit
    for pattern in \
      "$HOME"/.npm/_npx/*/node_modules/@deepseek-ai/dsh/lib/bin.js \
      "$HOME"/.npm/_npx/*/node_modules/.bin/dsh \
      "$HOME"/node_modules/@deepseek-ai/dsh/lib/bin.js \
      /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js \
      /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js
    do
      for hit in $pattern; do
        [ -e "$hit" ] && { candidate="$hit"; break; }
      done
      [ -n "$candidate" ] && break
    done
  fi

  if [ -n "$candidate" ] && [ -e "$candidate" ]; then
    DSH_BIN="$candidate"
    ok "$DSH_BIN"
    return 0
  fi

  # Nothing found. Say what was tried, so the next person does not have to guess
  # whether the tool is absent or the search was wrong.
  warn "could not find 'dsh'. Looked at:"
  say  "      \$COWORK_DSH_BIN           ${COWORK_DSH_BIN:-<unset>}"
  say  "      PATH                      $(command -v dsh 2>/dev/null || echo '<not on PATH>')"
  say  "      ~/.npm/_npx/*/…/dsh       $(ls -d "$HOME"/.npm/_npx/*/node_modules/@deepseek-ai/dsh 2>/dev/null | head -1 || echo '<none>')"
  say  ""
  say  "  If you have run 'npx @deepseek-ai/dsh' before, point at it directly:"
  say  "      COWORK_DSH_BIN=$(ls "$HOME"/.npm/_npx/*/node_modules/@deepseek-ai/dsh/lib/bin.js 2>/dev/null | head -1 || echo /path/to/dsh/lib/bin.js) ./setup.sh"
  die "the DeepSeek Harness ('dsh') was not found.
  Install it with:   npm install -g @deepseek-ai/dsh
  or point at it:    COWORK_DSH_BIN=/path/to/dsh/lib/bin.js ./setup.sh"
}

# The profile composes bundles that ship inside the dsh installation, so the
# installed profile needs a node_modules that can resolve them.
DSH_INSTALL_ROOT=""
find_dsh_install_root() {
  # Resolve symlinks first: an npm `.bin/dsh` is a symlink, so walking up from
  # the link's own directory two levels lands in the wrong place (it found
  # ~/.npm/_npx, which holds no packages).
  local real dir root
  real="$(readlink -f "$DSH_BIN" 2>/dev/null || echo "$DSH_BIN")"
  dir="$(dirname "$real")"                 # .../@deepseek-ai/dsh/lib
  dir="$(dirname "$dir")"                  # .../@deepseek-ai/dsh
  root="$(dirname "$(dirname "$dir")")"    # .../node_modules
  if [ -d "$root/@deepseek-ai/dsh-base" ]; then
    DSH_INSTALL_ROOT="$root"
    return 0
  fi
  # Fall back to searching upwards for wherever dsh-base actually lives.
  local probe="$dir"
  while [ "$probe" != "/" ]; do
    if [ -d "$probe/node_modules/@deepseek-ai/dsh-base" ]; then
      DSH_INSTALL_ROOT="$probe/node_modules"
      return 0
    fi
    if [ -d "$probe/@deepseek-ai/dsh-base" ]; then
      DSH_INSTALL_ROOT="$probe"
      return 0
    fi
    probe="$(dirname "$probe")"
  done
  DSH_INSTALL_ROOT=""
  # NOT a failure: the caller warns and continues. Returning non-zero here would
  # make `set -e` abort the whole install at this point, which is exactly what
  # happened — the profile had been copied, then the script died silently.
  return 0
}

# ── install ─────────────────────────────────────────────────────────────────

bundle_launcher() {
  step "Bundling the runtime launcher"
  # The panel cannot spawn the runtime (LibreOffice's embedded Python cannot), so
  # the launcher travels with the extension and the panel runs it on demand.
  # There is NO service to install and nothing to keep running by hand — the
  # earlier design's systemd unit was a second process and a second failure mode.
  if [ ! -f "$SRC/dsh/libreoffice/cowork/cowork-runtime.sh" ]; then
    warn "launcher missing from the source tree"; return 0
  fi
  ok "cowork-runtime.sh ships inside the .oxt"
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
      # `"${LO_ARGS[@]:-}"` expands to ONE EMPTY ARGUMENT when the array is
      # empty, which unopkg reads as a sub-command name:
      #   "Unknown sub-command: ''"
      # That only happens when no profile override is set, i.e. the normal case.
      # Branch instead of relying on a default expansion.
      if [ "${#LO_ARGS[@]}" -gt 0 ]; then
        out="$("$UNOPKG" "${LO_ARGS[@]}" "$@" 2>&1)"
      else
        out="$("$UNOPKG" "$@" 2>&1)"
      fi
      if [ $? -eq 0 ]; then printf '%s' "$out"; return 0; fi
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

  # The runtime is started on demand by the panel, so it is not expected to be
  # running now. Report whether its launcher is in place instead.
  local launcher="$SRC/dsh/libreoffice/cowork/cowork-runtime.sh"
  if [ -x "$launcher" ] || [ -f "$launcher" ]; then
    ok "the runtime launcher is installed; the panel starts it when needed"
  else
    warn "the runtime launcher is missing; the panel will not be able to start it"
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
bundle_launcher
verify

say ""
say "${G}${B}Done.${N}"
say ""
say "Next:"
say "  1. Start (or restart) LibreOffice and open a document."
say "  2. Open the Cowork deck in the sidebar — ${B}View ▸ Sidebar ▸ Cowork${N}."
say ""
say "  That is all: the panel starts the Cowork runtime itself the first time you"
say "  send a message. There is no service to install and nothing to keep running."
say ""
say "${DIM}Uninstall any time with:  setup.sh --uninstall${N}"
