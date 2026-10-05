# Security

## What this app touches

- **Reads** `~/.grok/auth.json` (your existing Grok Build / grok.com OIDC session).
- **Reads** the Grok Bot session already on this Mac (its secrets file and the Keychain item Grok Bot created) and decrypts that access token in memory.
- **Reads** local Grok Build session files under `~/.grok/sessions` for the fuel gauge.
- **Calls** xAI’s CLI chat proxy billing endpoints with the Build session token.
- **Calls** the Grok Bot dashboard API (`api2.cursor.sh`) for the Bot allowance and today’s token total.
- **Never** asks for your password or API key in a web form of its own.
- **Never** uploads usage data anywhere else. Network traffic is only those two APIs.

## What we do not do

- Store credentials beyond what Grok Build and Grok Bot already wrote locally
- Log tokens, refresh tokens, or Keychain material (logs are local: `~/Library/Logs/grok-build-usage.log`)
- Require a separate API key for either gauge

## Reporting issues

If you find a credential leak, unsafe logging, or a way this tool could exfiltrate
a session token, please open a private security advisory on the GitHub repo (or
email the maintainer if advisories are unavailable). Do not post live tokens in
public issues.
