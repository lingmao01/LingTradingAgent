#!/usr/bin/env bash
# Double-click launcher (macOS Finder): opens Terminal, starts the backend and
# the frontend via start.sh, and opens the frontend in the default browser.
# On Linux, run this from a file manager that executes .command files, or use
# ./start.sh --open directly.
cd "$(dirname "$0")" || exit 1
exec ./start.sh --open