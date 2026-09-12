#!/usr/bin/env bash
# Regression test: install the way a real remote user does.
#
# A genuine `curl | bash` has no local checkout, so setup.sh must download the
# repository tarball into a temp directory. Running the script from a directory
# that happens to contain the sources would silently test the wrong path.
#
# The script is copied somewhere neutral and executed there, so its own
# directory has no sources to find.
set -euo pipefail

WORKSPACE="${1:-/home/brandon/opt/libreoffice-cowork}"
NEUTRAL="$WORKSPACE/.neutral"
TESTHOME="$WORKSPACE/.curltest-real"

rm -rf "$NEUTRAL" "$TESTHOME"
mkdir -p "$NEUTRAL" "$TESTHOME/loprofile" "$TESTHOME/dshhome"

# Fetch the published script into a directory that holds nothing else.
curl -fsSL https://raw.githubusercontent.com/brandon-nuttall/libreoffice-cowork/main/setup.sh \
  -o "$NEUTRAL/setup.sh"

echo "### script placed in $NEUTRAL (no sources beside it) ###"
ls -A "$NEUTRAL"
echo

cd "$NEUTRAL"
DSH_HOME="$TESTHOME/dshhome" \
COWORK_LO_PROFILE="$TESTHOME/loprofile" \
COWORK_DSH_BIN=/home/brandon/.dsh/profiles/node_modules/@deepseek-ai/dsh/lib/bin.js \
  bash "$NEUTRAL/setup.sh" --yes

echo
echo "### did the downloaded tree actually get used? ###"
if [ -e "$TESTHOME/dshhome/profiles/libreoffice/cowork/uno_bridge.py" ]; then
  echo "PASS: profile + bridge installed from the downloaded archive"
  wc -l "$TESTHOME/dshhome/profiles/libreoffice/cowork/uno_bridge.py"
else
  echo "FAIL: nothing installed"
  exit 1
fi
