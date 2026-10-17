"""Load and refresh Grok Build OIDC credentials from ~/.grok/auth.json.

Tokens are never logged. A refresh writes the same file the TUI would write,
so billing keeps working after the access token expires and the TUI is closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests


DEFAULT_AUTH_PATH = Path.home() / ".grok" / "auth.json"
DEFAULT_VERSION_PATH = Path.home() / ".grok" / "version.json"
# Refresh a little early so a poll does not race the expiry.
_REFRESH_SKEW_SEC = 60.0


@dataclass(frozen=True)
class GrokAuth:
    """Subset of Grok Build auth entry needed for billing API calls."""

    key: str
    user_id: str
    team_id: Optional[str]
    auth_mode: Optional[str]
    expires_at: Optional[str]
    refresh_token: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    principal_type: Optional[str] = None
    principal_id: Optional[str] = None
    scope_key: Optional[str] = None

    @property
    def has_token(self) -> bool:
        return bool(self.key and self.user_id)

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.oidc_issuer and self.oidc_client_id and self.scope_key)


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

    scope_key, entry = _select_entry(raw)
    return _auth_from_entry(scope_key, entry)


def access_expired(
    auth: GrokAuth,
    *,
    now: Optional[datetime] = None,
    skew_sec: float = _REFRESH_SKEW_SEC,
) -> bool:
    """True when the access token is missing an expiry or is inside the skew window."""
    if not auth.expires_at:
        return False
    exp = _parse_time(auth.expires_at)
    if exp is None:
        return False
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() >= exp.timestamp() - skew_sec


def refresh_auth(
    auth: GrokAuth,
    *,
    path: Path = DEFAULT_AUTH_PATH,
    session: Optional[requests.Session] = None,
    now: Optional[datetime] = None,
) -> GrokAuth:
    """Rotate the access token the way Grok Build does, and persist it.

    Uses the OIDC discovery document and a ``refresh_token`` grant. The new
    access token, rotated refresh token, and expiry replace those fields in
    ``auth.json``. Every other field stays.
    """
    if not auth.can_refresh:
        raise AuthError("Grok session cannot be refreshed. Open Grok Build and run /login.")
    assert auth.oidc_issuer and auth.oidc_client_id and auth.refresh_token and auth.scope_key
    http = session or requests.Session()
    issuer = auth.oidc_issuer.rstrip("/")
    try:
        disc = http.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
        disc.raise_for_status()
        discovered = disc.json()
    except (requests.RequestException, ValueError) as exc:
        raise AuthError("Could not refresh the Grok session.") from exc
    endpoint = discovered.get("token_endpoint") if isinstance(discovered, dict) else None
    if not isinstance(endpoint, str) or not endpoint:
        raise AuthError("Could not refresh the Grok session.")

    form = {
        "grant_type": "refresh_token",
        "refresh_token": auth.refresh_token,
        "client_id": auth.oidc_client_id,
    }
    if auth.principal_type:
        form["principal_type"] = auth.principal_type
    if auth.principal_id:
        form["principal_id"] = auth.principal_id
    try:
        resp = http.post(endpoint, data=form, timeout=15)
    except requests.RequestException as exc:
        raise AuthError("Could not refresh the Grok session.") from exc
    if not resp.ok:
        raise AuthError("Grok session expired. Open Grok Build and run /login.")
    try:
        body = resp.json()
    except ValueError as exc:
        raise AuthError("Could not refresh the Grok session.") from exc
    if not isinstance(body, dict):
        raise AuthError("Could not refresh the Grok session.")
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError("Could not refresh the Grok session.")
    rotated = body.get("refresh_token")
    refresh_token = rotated if isinstance(rotated, str) and rotated else auth.refresh_token
    expires_at = _expiry_from_body(body, now=now) or auth.expires_at
    _persist_refresh(
        path,
        auth.scope_key,
        key=access,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    return GrokAuth(
        key=access,
        user_id=auth.user_id,
        team_id=auth.team_id,
        auth_mode=auth.auth_mode,
        expires_at=expires_at,
        refresh_token=refresh_token,
        oidc_issuer=auth.oidc_issuer,
        oidc_client_id=auth.oidc_client_id,
        principal_type=auth.principal_type,
        principal_id=auth.principal_id,
        scope_key=auth.scope_key,
    )


def _select_entry(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Prefer the OIDC entry. auth.json is keyed by issuer::client_id."""
    for key, entry in raw.items():
        if (
            isinstance(entry, dict)
            and entry.get("refresh_token")
            and entry.get("oidc_issuer")
            and entry.get("oidc_client_id")
        ):
            return str(key), entry
    key, entry = next(iter(raw.items()))
    if not isinstance(entry, dict):
        raise AuthError("Unexpected Grok auth shape.")
    return str(key), entry


def _auth_from_entry(scope_key: str, entry: dict[str, Any]) -> GrokAuth:
    key = entry.get("key") or ""
    user_id = entry.get("user_id") or ""
    if not key or not user_id:
        raise AuthError("Grok auth missing key or user_id. Run `grok login`.")

    def _text(name: str) -> Optional[str]:
        value = entry.get(name)
        if value is None or value == "":
            return None
        return str(value)

    return GrokAuth(
        key=str(key),
        user_id=str(user_id),
        team_id=_text("team_id"),
        auth_mode=_text("auth_mode"),
        expires_at=_text("expires_at"),
        refresh_token=_text("refresh_token"),
        oidc_issuer=_text("oidc_issuer"),
        oidc_client_id=_text("oidc_client_id"),
        principal_type=_text("principal_type"),
        principal_id=_text("principal_id"),
        scope_key=scope_key,
    )


def _persist_refresh(
    path: Path,
    scope_key: str,
    *,
    key: str,
    refresh_token: str,
    expires_at: Optional[str],
) -> None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError("Could not store the refreshed Grok session.") from exc
    if not isinstance(raw, dict):
        raise AuthError("Could not store the refreshed Grok session.")
    entry = raw.get(scope_key)
    if not isinstance(entry, dict):
        raise AuthError("Could not store the refreshed Grok session.")
    entry["key"] = key
    entry["refresh_token"] = refresh_token
    if expires_at:
        entry["expires_at"] = expires_at
    payload = (json.dumps(raw, indent=2) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as exc:
        raise AuthError("Could not store the refreshed Grok session.") from exc
    try:
        view = payload
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise AuthError("Could not store the refreshed Grok session.") from exc
    os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise AuthError("Could not store the refreshed Grok session.") from exc
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _expiry_from_body(body: dict[str, Any], *, now: Optional[datetime]) -> Optional[str]:
    raw = body.get("expires_in")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    exp = moment.astimezone(timezone.utc) + timedelta(seconds=float(raw))
    text = exp.isoformat()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def _parse_time(raw: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def client_version(path: Path = DEFAULT_VERSION_PATH) -> str:
    if not path.is_file():
        return "0.0.0"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("version") or "0.0.0")
    except (OSError, json.JSONDecodeError):
        return "0.0.0"
