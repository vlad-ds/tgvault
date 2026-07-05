#!/usr/bin/env bash
# tgvault installer for macOS / Linux. No Git or Python knowledge needed.
set -euo pipefail

REPO="${TGVAULT_REPO:-git+https://github.com/vlad-ds/tgvault}"

echo "Installing tgvault..."

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv tool install --force --python 3.12 "$REPO"

echo
echo "Done! Next steps:"
echo "  1. Open a NEW terminal window (so the tgvault command is found)"
echo "  2. Run: tgvault login      (scan the QR code with the Telegram app)"
echo "  3. Run: tgvault chats      (see your chats)"
echo "  4. Run: tgvault watch \"<chat name>\"   then   tgvault sync"
