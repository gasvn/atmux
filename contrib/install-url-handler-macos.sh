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
BUNDLE_ID="com.autotmux.urlhandler"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

# The AppleScript lives in autotmux.urlhandler, not here: the URL is untrusted
# input that crosses an AppleScript literal, a shell word and argv on its way to
# the attach, and generating it in Python puts that quoting under test. ATMUX_BIN
# points the applet at the launcher the user invokes rather than whatever this
# process was exec'd into.
if ! ATMUX_BIN="${ATMUX}" "${ATMUX}" --print-url-handler \
        > "${BUILD}/handler.applescript" 2>"${BUILD}/err"; then
    echo "Could not generate the handler script:" >&2
    cat "${BUILD}/err" >&2
    echo "Is ${ATMUX} an AutoTmux 0.7.0 or newer install?" >&2
    exit 1
fi

mkdir -p "${HOME}/Applications"
rm -rf "${APP}"
osacompile -o "${APP}" "${BUILD}/handler.applescript"

# osacompile writes a plain applet with no bundle identifier. macOS records
# automation consent per identifier, so without one the permission can be
# neither prompted for nor remembered -- opening a session then fails with
# "Not authorized to send Apple events", and no dialog ever appears.
#
# LSBackgroundOnly is deliberately NOT set for the same reason: a background-
# only app cannot present the consent dialog.
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes array' \
    -c 'Add :CFBundleURLTypes:0 dict' \
    -c 'Add :CFBundleURLTypes:0:CFBundleURLName string AutoTmux' \
    -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes array' \
    -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string atmux' \
    -c "Add :CFBundleIdentifier string ${BUNDLE_ID}" \
    "${APP}/Contents/Info.plist" >/dev/null

# Re-sign after editing the bundle, or the identity TCC keys on is stale.
codesign --force --sign - "${APP}" >/dev/null 2>&1 || true

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
