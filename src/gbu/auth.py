"""Load Grok Build OIDC credentials from ~/.grok/auth.json (no secrets logged)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_AUTH_PATH = Path.home() / ".grok" / "auth.json"
DEFAULT_VERSION_PATH = Path.home() / ".grok" / "version.json"


@dataclass(frozen=True)
class GrokAuth:
    """Subset of Grok Build auth entry needed for billing API calls."""

    key: str
    user_id: str
    team_id: Optional[str]
    auth_mode: Optional[str]
    expires_at: Optional[str]

    @property
    def has_token(self) -> bool:
        return bool(self.key and self.user_id)


class AuthError(RuntimeError):
    """Raised when auth.json is missing, empty, or unusable."""


def load_auth(path: Path = DEFAULT_AUTH_PATH) -> GrokAuth:
    if not path.is_file():
        raise AuthError(
            f"No Grok auth at {path}. Open Grok Build and run /login first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"Could not read Grok auth: {exc}") from exc

    if not isinstance(raw, dict) or not raw:
        raise AuthError("Grok auth file is empty. Run `grok login`.")

    # auth.json is keyed by issuer::client_id → entry
    entry = next(iter(raw.values()))
    if not isinstance(entry, dict):
        raise AuthError("Unexpected Grok auth shape.")

    key = entry.get("key") or ""
    user_id = entry.get("user_id") or ""
    if not key or not user_id:
        raise AuthError("Grok auth missing key or user_id. Run `grok login`.")

    return GrokAuth(
        key=str(key),
        user_id=str(user_id),
        team_id=str(entry["team_id"]) if entry.get("team_id") else None,
        auth_mode=str(entry["auth_mode"]) if entry.get("auth_mode") else None,
        expires_at=str(entry["expires_at"]) if entry.get("expires_at") else None,
    )


def client_version(path: Path = DEFAULT_VERSION_PATH) -> str:
    if not path.is_file():
        return "0.0.0"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("version") or "0.0.0")
    except (OSError, json.JSONDecodeError):
        return "0.0.0"
