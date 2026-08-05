#!/bin/bash
# Register an atmux:// URL handler on macOS, so a link in Slack (or anywhere
# else) can open a session.
#
# The handler is deliberately thin. It hands the URL, quoted, to
# `atmux --open-url`, which validates it and dispatches the node and session
# as argv. Nothing from the URL reaches a shell before that check: the link
# arrives from a chat message, so anyone who can post to the channel can craft
# one.
#
# Usage:  contrib/install-url-handler-macos.sh [/path/to/atmux]

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This installer is macOS-only (it registers a LaunchServices handler)." >&2
    exit 1
fi

ATMUX="${1:-$(command -v atmux || true)}"
if [[ -z "${ATMUX}" || ! -x "${ATMUX}" ]]; then
    echo "Could not find an executable atmux. Pass its path:" >&2
    echo "  $0 /path/to/atmux" >&2
    exit 1
fi
ATMUX="$(cd "$(dirname "${ATMUX}")" && pwd)/$(basename "${ATMUX}")"

APP="${HOME}/Applications/AutoTmux URL Handler.app"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

# Which terminal to open. iTerm2 if present, else Terminal.app -- the attach
# needs a real TTY, so this cannot run under `do shell script`.
if [[ -d "/Applications/iTerm.app" ]]; then
    OPEN_CMD='tell application "iTerm"
            activate
            create window with default profile command theCommand
        end tell'
else
    OPEN_CMD='tell application "Terminal"
            activate
            do script theCommand
        end tell'
fi

cat > "${BUILD}/handler.applescript" <<APPLESCRIPT
on open location this_URL
    set theCommand to quoted form of "${ATMUX}" & " --open-url " & quoted form of this_URL
    ${OPEN_CMD}
end open location
APPLESCRIPT

mkdir -p "${HOME}/Applications"
rm -rf "${APP}"
osacompile -o "${APP}" "${BUILD}/handler.applescript"

# osacompile writes a plain applet; declare the scheme it should receive.
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes array' \
    -c 'Add :CFBundleURLTypes:0 dict' \
    -c 'Add :CFBundleURLTypes:0:CFBundleURLName string AutoTmux' \
    -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes array' \
    -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string atmux' \
    -c 'Add :LSBackgroundOnly bool true' \
    "${APP}/Contents/Info.plist" >/dev/null

# Tell LaunchServices about it now rather than at some later rescan.
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"${LSREGISTER}" -f "${APP}"

echo "Installed: ${APP}"
echo "Handler runs: ${ATMUX} --open-url <url>"
echo
echo "Test it with:  open 'atmux://attach/NODE/SESSION'"
echo "Then enable links in the daemon config on the login node:"
echo "  [notify]"
echo "  attach_link = true"
