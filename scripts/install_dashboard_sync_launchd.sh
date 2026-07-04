#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.joshmackwell.letterboxd-dashboard-sync"
PLIST_SRC="$PROJECT_DIR/launchd/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/scripts/sync_dashboard_to_icloud.sh"

sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DEST"

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"

echo "Installed and loaded $LABEL (runs daily at 09:00, after the GitHub Actions run)."
echo "Logs: $PROJECT_DIR/logs/dashboard-sync.log and dashboard-sync.error.log"
echo "Check status with: launchctl list | grep $LABEL"
echo "Run immediately with: launchctl start $LABEL"
