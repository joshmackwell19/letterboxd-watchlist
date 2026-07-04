#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.joshmackwell.letterboxd-watchlist"
PLIST_SRC="$PROJECT_DIR/launchd/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/scripts/run.sh"

sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DEST"

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"

echo "Installed and loaded $LABEL (runs daily at 08:00)."
echo "Logs: $PROJECT_DIR/logs/stdout.log and stderr.log"
echo "Check status with: launchctl list | grep $LABEL"
echo "Run immediately with: launchctl start $LABEL"
