#!/usr/bin/env bash
# Native launcher for install.py (see its own docstring) on macOS/Linux -
# bash is the same tool on both, so one script covers both; Windows gets
# its own install.bat since it has no bash by default.
#
# Only job here is finding python3 and running install.py from this
# script's own directory regardless of where it was invoked from.
# install.py handles everything else itself, including elevating to root
# when it needs to (see its _self_elevate()) - no sudo prefix needed here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

case "$(uname -s)" in
    Darwin|Linux) ;;
    *)
        echo "install.sh is for macOS/Linux. On Windows, run install.bat instead."
        exit 1
        ;;
esac

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "python3 not found. Install it first:"
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "    xcode-select --install     (ships a python3)"
        echo "    or: brew install python3"
    else
        echo "    sudo apt install python3   (Debian/Ubuntu)"
        echo "    sudo dnf install python3   (Fedora)"
    fi
    exit 1
fi

exec "$PYTHON" install.py "$@"
