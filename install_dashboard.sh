#!/bin/bash
# One-time setup: builds ~/Applications/Helm.app. Opening it starts the dashboard and opens it in your browser;
# the server shuts itself down when you close the page, so nothing keeps running. --uninstall removes it.
cd "$(dirname "$0")"
APP=~/Applications/Helm.app
[ "$1" = "--uninstall" ] && { rm -rf "$APP"; echo "Helm.app removed"; exit 0; }

SCRIPT=$(mktemp).applescript
cat > "$SCRIPT" <<APPLESCRIPT
on isUp()
	try
		do shell script "curl -s -o /dev/null --max-time 1 http://127.0.0.1:8000/"
		return true
	on error
		return false
	end try
end isUp

on run
	if not isUp() then
		do shell script "cd " & quoted form of "$PWD" & "; nohup .venv/bin/python bot.py web --exit-when-closed >> dashboard.log 2>&1 < /dev/null &"
		repeat 40 times
			delay 0.5
			if isUp() then exit repeat
		end repeat
	end if
	open location "http://localhost:8000"
end run
APPLESCRIPT

mkdir -p ~/Applications
osacompile -o "$APP" "$SCRIPT" && echo "built $APP (open it, or drag it to your Dock)"
rm -f "$SCRIPT"
