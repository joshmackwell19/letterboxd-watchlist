#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Letterboxd Dashboard"

cd "$PROJECT_DIR"
git pull --quiet

mkdir -p "$ICLOUD_DIR"
cp "$PROJECT_DIR/dashboard.html" "$ICLOUD_DIR/dashboard.html"

echo "Synced dashboard.html to iCloud Drive at $(date)"
