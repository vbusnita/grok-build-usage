# Grok Build Usage

**Always-on macOS menu bar + floating HUD for [Grok Build](https://grok.com) and Grok Bot usage.**

The overlay has three blocks:

- **Fuel** — tokens burned by the live session. The number is the burn. The bar and the word (quiet, easy, steady, hard) are the pace, not a percent of the weekly allowance.
- **The plan** — one shared pool (SuperGrok Plus, for example) for Build, Chat, Imagine, and Voice, with that pool’s own reset. A product you have not used since the reset shows 0%.
- **Grok Bot** — its own allowance and its own reset. The plan name stays on the shared pool.

The menu bar title stays short: `GBU · …% · Bot …%`.

Works with **your** logins. The shared pool reuses the session Grok Build already stores on disk. The Bot row reuses the signed-in Grok Bot Mac session. No separate API key.

> Unofficial community tool. Not affiliated with xAI. Billing endpoints can change; if they do, open an issue.

## Overlay

Chrome-free floating panel, top to bottom: Fuel, the plan pool (percent, reset, and the Build / Chat / Imagine / Voice mix), then Grok Bot with its own percent and reset. Credits, pay as you go, and auto top-up appear on the plan note when the account has them.

Toggle the overlay from the menu bar anytime (**Hide / Show Overlay**).

## Requirements

| Need | Notes |
|------|--------|
| **macOS** | Menu bar + AppKit HUD |
| **Python 3.10+** | 3.11/3.12 recommended |
| **Grok Build logged in** | Run `grok login` (or `/login` in the TUI) once so `~/.grok/auth.json` exists |

## Install (anyone)

```bash
git clone https://github.com/vbusnita/grok-build-usage.git
cd grok-build-usage
./scripts/install-app.sh --login --open
```

That will:

1. Create a local `.venv` and install this package  
2. Build **`~/Applications/Grok Build Usage.app`** (menu-bar agent — **no Dock icon**; Finder shows the usage-bars app icon)  
3. Bundle the monochrome three-bar menu-bar glyph + `AppIcon.icns` from `src/gbu/assets/`  
4. Embed a named Python runtime inside the app (System Settings may still list a sticky **python3.11** allow-list row from early installs — keep it **ON**)  
5. Register a **LaunchAgent** so it starts at login (`--login`)  
6. Launch it (`--open`)

Then look for the **three-bar** glyph + **`GBU · …% · Bot …%`** in the menu bar. The menu also lists Fuel, the plan pool, and Grok Bot. Use **Hide / Show Overlay**, **Refresh Now**, **Open Grok Usage…**, or **Quit**.

**Stays until you Quit:** the LaunchAgent restarts the process if it crashes (non-zero exit). **Quit** from the menu bar exits cleanly (exit 0) and does **not** auto-restart until the next login or you open the app again. The menu-bar item also self-heals if macOS parks it (sleep/wake, display changes); if repair fails repeatedly it restarts itself.

**Re-open after Quit:** double-click **`~/Applications/Grok Build Usage.app`** (or Spotlight). The launcher re-dispatches through the LaunchAgent — do not expect a Dock icon (agent app). Install with `--login` so reopen always works.

**Background activity / auth prompt:** macOS may ask once (password or Touch ID) to allow the login item. Approve **Grok Build Usage** under System Settings → General → **Login Items & Extensions** and leave it on. Opening the app again should not re-prompt; we only start the agent when it is not already running.

**Menu Bar allow-list:** on macOS that already ran an early install, the toggle is often still labeled **python3.11**. Keep that **ON** — it is this app.

### Without login-at-start

```bash
./scripts/install-app.sh --open
```

### Uninstall

```bash
./scripts/uninstall-app.sh
```

Removes the `.app` and LaunchAgent(s) (including a legacy personal label if present). Leaves your clone, `.venv`, Grok login (`~/.grok`), and log file.

### Logs

`~/Library/Logs/grok-build-usage.log`

## CLI (optional)

```bash
source .venv/bin/activate
gbu              # menu bar + overlay
gbu --hidden     # menu bar only
gbu --once       # print one snapshot, no UI
gbu --poll 20    # refresh every 20s
```

## How it works (for your account)

1. **Auth** — reads the OIDC session from `~/.grok/auth.json` written by Grok Build.  
2. **Billing** — `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` (same family of calls Build uses for `/usage`). The plan percent is the shared pool. Build, Chat, Imagine, and Voice are the mix under that reset.  
3. **Auto top-up** — optional `…/auto-topup-rule`.  
4. **Grok Bot** — decrypts the local Grok Bot session in memory and reads its own allowance (`GetSandUsageStatus`) plus today’s token total. The token is not logged. If Bot is signed out, the plan gauge still shows.  
5. **Fuel** — sums `totalTokens` from live `~/.grok/sessions` usage files. Bot tokens count only after they rise past the first sample in this sitting.  
6. **UI** — menu bar + floating always-on-top HUD. Billing polls about every 45s. Fuel rereads local session files about every 5s.

No credentials are sent anywhere except xAI (the plan pool) and the Grok Bot API (the Bot allowance). This app does not store your password.

If auth expires, the HUD says so — open Grok Build and `/login` again.

## Privacy & security

See [SECURITY.md](SECURITY.md). Short version: local session reuse, local logs, xAI-only network.

## Development

```bash
git clone https://github.com/vbusnita/grok-build-usage.git
cd grok-build-usage
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Project layout

```
src/gbu/
  auth.py       # ~/.grok/auth.json
  billing.py    # CLI proxy billing fetch
  bot_usage.py  # Grok Bot allowance and today's token total
  fuel.py       # live session fuel (tokens and pace)
  models.py     # UsageSnapshot, plan pools
  hud.py        # floating chrome-free HUD
  app.py        # rumps menu bar + status glyph
  __main__.py   # CLI
  assets/       # AppIcon.png + MenuBarTemplate(@2x).png
scripts/
  install-app.sh    # venv, .app, icons, optional LaunchAgent
  uninstall-app.sh  # stop agent, remove .app + LaunchAgent
```

## Disclaimer

This project is **not** an official xAI product. It depends on Grok Build’s local auth file and billing HTTP shapes that may change without notice. Use at your own risk.

## License

[MIT](LICENSE) © Victor Busnita
