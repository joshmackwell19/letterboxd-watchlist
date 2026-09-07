#!/bin/bash
set -euo pipefail

ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Letterboxd Dashboard"
PAGES_URL="https://joshmackwell19.github.io/letterboxd-watchlist/"

# dashboard.html is no longer committed to git (see daily.yml/
# regenerate-dashboard.yml) — pulled straight from the public Pages
# deployment instead, which is the same content and updates on the same
# schedule, without needing a git operation at all.
mkdir -p "$ICLOUD_DIR"
curl -fsSL "$PAGES_URL" -o "$ICLOUD_DIR/dashboard.html"

echo "Synced dashboard.html to iCloud Drive at $(date)"
