#!/usr/bin/env bash
# Remove Grok Build Usage app + LaunchAgent.
#
# Leaves: project clone, .venv, ~/.grok auth, and log file.
set -euo pipefail

APP_NAME="Grok Build Usage"
APP_DIR="${HOME}/Applications/${APP_NAME}.app"
BUNDLE_ID="app.grokbuild.usage"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${BUNDLE_ID}.plist"
DOMAIN="gui/$(id -u)"

# 1) Stop LaunchAgents first so KeepAlive cannot respawn after we kill.
#    Current neutral id + legacy personal id (pre-open-source).
for id in "${BUNDLE_ID}" "com.vbusnita.grok-build-usage"; do
  launchctl bootout "${DOMAIN}/${id}" 2>/dev/null || true
  rm -f "${HOME}/Library/LaunchAgents/${id}.plist"
done

# 2) Kill any remaining runtime / launcher processes.
#    Prefer exact paths over broad pkill -f patterns.
if [[ -x "${APP_DIR}/Contents/MacOS/GrokBuildUsage" ]]; then
  pkill -f "${APP_DIR}/Contents/MacOS/GrokBuildUsage" 2>/dev/null || true
fi
if [[ -x "${APP_DIR}/Contents/MacOS/GrokBuildUsageApp" ]]; then
  pkill -f "${APP_DIR}/Contents/MacOS/GrokBuildUsageApp" 2>/dev/null || true
fi
# Dev / CLI runs outside the .app
pkill -f "[Pp]ython.*-m gbu" 2>/dev/null || true

# 3) Remove the app bundle
rm -rf "${APP_DIR}"

echo "Removed ${APP_DIR} and LaunchAgent ${BUNDLE_ID}."
echo "(Project source, venv, logs, and Grok login are left alone.)"
echo "Optional: rm ~/Library/Logs/grok-build-usage.log"
