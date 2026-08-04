#!/usr/bin/env bash
# Remove Grok Build Usage app + LaunchAgent.
set -euo pipefail

APP_NAME="Grok Build Usage"
APP_DIR="${HOME}/Applications/${APP_NAME}.app"
BUNDLE_ID="app.grokbuild.usage"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${BUNDLE_ID}.plist"

# Stop running instances
pkill -f "python.*-m gbu" 2>/dev/null || true
pkill -f "GrokBuildUsage -m gbu" 2>/dev/null || true
pkill -f "Grok Build Usage.app/Contents/MacOS" 2>/dev/null || true

# Current + legacy personal bundle id (pre-open-source)
for id in "${BUNDLE_ID}" "com.vbusnita.grok-build-usage"; do
  launchctl bootout "gui/$(id -u)/${id}" 2>/dev/null || true
  rm -f "${HOME}/Library/LaunchAgents/${id}.plist"
done
rm -rf "${APP_DIR}"

echo "Removed ${APP_DIR} and LaunchAgent ${BUNDLE_ID}."
echo "(Project source is left alone.)"
