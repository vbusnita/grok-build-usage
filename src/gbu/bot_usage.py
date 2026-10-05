"""Grok Bot allowance from the signed-in Grok Bot Mac session.

Grok Bot bills on its own pool (Cursor sand usage), separate from the Grok
Build weekly credit bar. This reads the local Grok Bot session the same way
``scripts/grok-bot/grokbot.py`` does and calls DashboardService/GetSandUsageStatus.
Tokens stay in memory and are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from gbu.models import UsageSlice, _parse_period_end

log = logging.getLogger(__name__)

SECRETS_PATH = Path.home() / "Library/Application Support/Grok Bot/sand-secrets.json"
STATUS_PATH = Path.home() / "Library/Application Support/Grok Bot/desktop-status.json"
BACKEND = "https://api2.cursor.sh"
SERVICE = "aiserver.v1.DashboardService"
METHOD = "GetSandUsageStatus"
TIMEOUT_SEC = 12


class BotUsageError(RuntimeError):
    """Grok Bot usage could not be read. Message is safe to show."""


def slice_from_sand_status(payload: dict[str, Any]) -> UsageSlice:
    """Map GetSandUsageStatus JSON to a breakdown row."""
    if not isinstance(payload, dict):
        raise BotUsageError("Grok Bot usage response was empty.")

    pct_raw = payload.get("usagePercent")
    if pct_raw is None:
        pct_raw = payload.get("usage_percent")
    pct: Optional[float]
    try:
        pct = max(0.0, min(100.0, float(pct_raw)))
    except (TypeError, ValueError):
        pct = None

    plan = payload.get("grokPlanLabel") or payload.get("grok_plan_label") or ""
    if not isinstance(plan, str):
        plan = ""
    plan = plan.strip()

    reset_raw = payload.get("nextResetTimestampUtc") or payload.get("next_reset_timestamp_utc")
    reset = _parse_period_end(reset_raw if isinstance(reset_raw, str) else None)

    note = None
    if payload.get("hasAvailableUsage") is False and pct is None:
        note = "no allowance"
    if pct is None and not reset and not plan and not note:
        raise BotUsageError("Grok Bot usage did not include a percent.")
    return UsageSlice(
        label="Grok Bot",
        usage_pct=pct,
        resets=reset,
        detail=note,
        plan=plan or None,
    )


def fetch_bot_slice() -> UsageSlice:
    """Live Grok Bot row. Failures become a row with no percent."""
    try:
        payload = _fetch_status()
        return slice_from_sand_status(payload)
    except BotUsageError as exc:
        log.info("grok bot usage unavailable: %s", exc)
        return UsageSlice(label="Grok Bot", usage_pct=None, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — HUD must still render Build usage
        log.info("grok bot usage failed: %s", type(exc).__name__)
        return UsageSlice(label="Grok Bot", usage_pct=None, detail="usage unavailable")


def token_total_from_spend(payload: dict[str, Any]) -> int:
    """Sum totalTokens on a GetDailySpendByCategory payload."""
    rows = payload.get("dailySpend") or payload.get("daily_spend") or []
    if not isinstance(rows, list):
        return 0
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("totalTokens")
        if raw is None:
            raw = row.get("total_tokens")
        try:
            total += max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return total


def fetch_bot_token_total() -> Optional[int]:
    """Today's Grok Bot token total, for measuring burn since the last sample.

    This is not the plan allowance. Failures return None so the fuel gauge
    still reads Grok Build.
    """
    try:
        now = datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        payload = _post_dashboard(
            "GetDailySpendByCategory",
            {
                "periodStartMs": str(int(start.timestamp() * 1000)),
                "periodEndMs": str(int(now.timestamp() * 1000)),
                "groupBy": "GROK_BOT",
                "spendType": "ALL",
                "clientType": "grok-bot",
                "products": ["sand"],
                "includeCreditUsage": True,
            },
        )
        return token_total_from_spend(payload)
    except BotUsageError as exc:
        log.info("grok bot fuel unavailable: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.info("grok bot fuel failed: %s", type(exc).__name__)
        return None


def _fetch_status() -> dict[str, Any]:
    return _post_dashboard(METHOD, {})


def _post_dashboard(method: str, body: dict[str, Any]) -> dict[str, Any]:
    token = _decrypt_access_token()
    url = f"{BACKEND}/{SERVICE}/{method}"
    raw_body = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=raw_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-cursor-client-type": "sand",
            "x-cursor-client-version": _client_version(),
            "x-sand-box-namespace": "prod",
            "x-ghost-mode": "false",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise BotUsageError(f"Grok Bot usage HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise BotUsageError("Grok Bot usage request failed") from exc
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise BotUsageError("Grok Bot usage response was not JSON") from exc
    if not isinstance(data, dict):
        raise BotUsageError("Grok Bot usage response was not an object")
    return data


def _client_version() -> str:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "0.66.0"
    version = data.get("appVersion") if isinstance(data, dict) else None
    return str(version) if version else "0.66.0"


def _decrypt_access_token() -> str:
    """Decrypt the Grok Bot Cursor access token. Never log the result."""
    if not SECRETS_PATH.is_file():
        raise BotUsageError("Grok Bot is not signed in on this Mac.")
    try:
        secrets = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BotUsageError("Could not read the Grok Bot session.") from exc
    if not isinstance(secrets, dict):
        raise BotUsageError("Grok Bot session file is unusable.")

    stored = secrets.get("cursor-access-token")
    if not (isinstance(stored, str) and stored.startswith("scoped:v1:")):
        accounts_raw = secrets.get("cursor-accounts")
        if isinstance(accounts_raw, str):
            try:
                accounts = json.loads(accounts_raw)
            except json.JSONDecodeError as exc:
                raise BotUsageError("Grok Bot account blob is unusable.") from exc
            active = accounts.get("active") if isinstance(accounts, dict) else None
            nested = {}
            if isinstance(accounts, dict):
                bucket = accounts.get("accounts") or {}
                if isinstance(bucket, dict):
                    nested = bucket.get(active) or {}
            if isinstance(nested, dict):
                stored = nested.get("cursor-access-token")
    if not isinstance(stored, str) or not stored:
        raise BotUsageError("Grok Bot access token is missing.")

    if stored.startswith("scoped:v1:"):
        rest = stored[len("scoped:v1:") :]
        try:
            raw = base64.b64decode(rest[rest.index(":") + 1 :])
        except (ValueError, IndexError) as exc:
            raise BotUsageError("Grok Bot access token envelope is invalid.") from exc
    else:
        try:
            raw = base64.b64decode(stored)
        except Exception as exc:  # noqa: BLE001
            raise BotUsageError("Grok Bot access token envelope is invalid.") from exc
    if not raw.startswith(b"v10"):
        raise BotUsageError("Grok Bot access token envelope is invalid.")

    try:
        password = subprocess.check_output(
            [
                "security",
                "find-generic-password",
                "-s",
                "Grok Bot Safe Storage",
                "-a",
                "Grok Bot Key",
                "-w",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).rstrip("\n")
    except subprocess.CalledProcessError as exc:
        raise BotUsageError("Could not read Grok Bot Keychain item.") from exc

    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, dklen=16)
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-d",
            "-K",
            key.hex(),
            "-iv",
            (b" " * 16).hex(),
            "-nopad",
        ],
        input=raw[3:],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise BotUsageError("Could not decrypt the Grok Bot session.")
    plaintext = proc.stdout
    pad = plaintext[-1]
    if pad < 1 or pad > 16 or plaintext[-pad:] != bytes([pad]) * pad:
        raise BotUsageError("Could not decrypt the Grok Bot session.")
    try:
        return plaintext[:-pad].decode()
    except UnicodeDecodeError as exc:
        raise BotUsageError("Could not decrypt the Grok Bot session.") from exc
