#!/usr/bin/env bash
#
# Run every check. One command, no flags to remember.
#
# Written after a run of failures that should have been caught earlier. The
# component statics check had been reporting `CoworkUIElement._scroll_to_newest()`
# is undefined — correctly — while I ran it as `python3 tests/test-component-statics.py
# >/dev/null 2>&1 && echo "statics: PASS"` and read only the exit status of the
# `&&` chain. The message that would have saved a rebuild-restart-capture cycle was
# being thrown away by my own redirect.
#
# So: this runs everything, in the foreground, with output visible.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

FAILED=()

run() {
  local name="$1"; shift
  printf '\n\033[1m── %s\033[0m\n' "$name"
  if "$@"; then
    return 0
  fi
  FAILED+=("$name")
}

run "component statics"  python3 tests/test-component-statics.py
run "layout geometry"    python3 tests/test-layout.py
run "block model"        python3 tests/test-markdown.py
run "panel wiring"       python3 tests/test-panel-wiring.py
# Needs a live runtime; says so and exits 0 when there is none, so it does not
# make the suite depend on one being up.
run "conversation"       python3 tests/test-conversation.py
# Needs an office with the extension AND a runtime, both targeting the same
# office. Says so and exits 0 when they are not up.
run "live edit + undo"   python3 tests/test-live-edit.py

printf '\n\033[1m── installer syntax\033[0m\n'
if bash -n setup.sh && bash -n tests/sandbox-ui.sh \
   && bash -n dsh/libreoffice/cowork/cowork-runtime.sh; then
  echo "  ok   shell scripts parse"
else
  echo "  FAIL shell scripts"
  FAILED+=("shell syntax")
fi

printf '\n'
if [ "${#FAILED[@]}" -gt 0 ]; then
  printf '\033[31mFAILED: %s\033[0m\n' "$(IFS=', '; echo "${FAILED[*]}")"
  exit 1
fi
printf '\033[32mAll checks passed.\033[0m\n'
