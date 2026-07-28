# Security

## What this app touches

- **Reads** `~/.grok/auth.json` (your existing Grok Build / grok.com OIDC session).
- **Calls** xAI’s CLI chat proxy billing endpoints with that session token.
- **Never** asks for your password or API key in a web form of its own.
- **Never** uploads usage data to a third party — all network traffic is to xAI.

## What we do not do

- Store credentials outside of what Grok Build already wrote locally
- Log tokens or refresh tokens (logs are local: `~/Library/Logs/grok-build-usage.log`)
- Require a separate xAI API key for the account-pool gauge

## Reporting issues

If you find a credential leak, unsafe logging, or a way this tool could exfiltrate
a session token, please open a private security advisory on the GitHub repo (or
email the maintainer if advisories are unavailable). Do not post live tokens in
public issues.
