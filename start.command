#!/bin/sh
# macOS double-click launcher.
# Finder opens .command files in Terminal; the window stays open so errors can be read.
#
#   chmod +x start.command     (once, after copying the folder)

cd "$(dirname "$0")" || exit 1

sh ./start.sh "$@"
status=$?

echo
printf "Service stopped (exit %s). Press Enter to close this window." "$status"
read -r _

exit "$status"
