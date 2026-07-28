#!/usr/bin/env bash
# Remove Grok Build Usage app + LaunchAgent.
set -euo pipefail

APP_NAME="Grok Build Usage"
APP_DIR="${HOME}/Applications/${APP_NAME}.app"
BUNDLE_ID="com.vbusnita.grok-build-usage"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${BUNDLE_ID}.plist"

# Stop running instances
pkill -f "python.*-m gbu" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${BUNDLE_ID}" 2>/dev/null || true
rm -f "${LAUNCH_AGENT_PLIST}"
rm -rf "${APP_DIR}"

echo "Removed ${APP_DIR} and LaunchAgent ${BUNDLE_ID}."
echo "(Project source at ~/projects/grok-build-usage is left alone.)"
