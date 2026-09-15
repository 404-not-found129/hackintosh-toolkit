#!/usr/bin/env bash
# Launcher for hackintosh_setup.py on macOS/Linux.
#
# Handles the boring parts: makes sure python3 exists, runs from this
# script's own directory regardless of where it was invoked from, and
# re-execs with sudo since partitioning needs root - hackintosh_setup.py
# itself still does all the real work (hardware detection, the destructive-
# disk confirmation, EFI build).
#
# Windows: there's no .sh equivalent - run `python hackintosh_setup.py`
# directly from an elevated PowerShell instead (see README.md).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

case "$(uname -s)" in
    Darwin|Linux) ;;
    *)
        echo "install.sh is for macOS/Linux. On Windows, run:"
        echo "    python hackintosh_setup.py"
        echo "from an elevated PowerShell instead - see README.md."
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

if [ "$(id -u)" -ne 0 ]; then
    echo "Partitioning needs root - re-running with sudo..."
    exec sudo "$PYTHON" hackintosh_setup.py "$@"
fi

exec "$PYTHON" hackintosh_setup.py "$@"
