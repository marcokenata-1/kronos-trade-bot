#!/bin/bash
# One-time setup: start the dashboard at login and keep it running (macOS launchd). Undo with --uninstall.
cd "$(dirname "$0")"
PLIST=~/Library/LaunchAgents/com.kronos.dashboard.plist
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null
[ "$1" = "--uninstall" ] && { rm -f "$PLIST"; echo "dashboard agent removed"; exit 0; }

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kronos.dashboard</string>
  <key>ProgramArguments</key><array>
    <string>$PWD/.venv/bin/python</string><string>bot.py</string><string>web</string>
  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/dashboard.log</string>
  <key>StandardErrorPath</key><string>$PWD/dashboard.log</string>
</dict></plist>
PLIST

launchctl bootstrap "$DOMAIN" "$PLIST" && echo "dashboard running at http://localhost:8000 (restart after editing bot.py: launchctl kickstart -k $DOMAIN/com.kronos.dashboard)"
