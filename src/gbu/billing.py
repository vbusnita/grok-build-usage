"""Fetch Grok Build credits / usage from the CLI chat proxy billing endpoint.

Mirrors the agent extension path in xai-grok-shell:
  GET {proxy}/billing?format=credits
  GET {proxy}/auto-topup-rule

Auth: Bearer from ~/.grok/auth.json + X-XAI-Token-Auth: xai-grok-cli
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from gbu.auth import AuthError, GrokAuth, client_version, load_auth
from gbu.models import UsageSnapshot, snapshot_from_billing

log = logging.getLogger(__name__)

DEFAULT_PROXY_BASE = "https://cli-chat-proxy.grok.com/v1"
TOKEN_AUTH_HEADER_VALUE = "xai-grok-cli"
CLIENT_MODE_HEADER = "x-grok-client-mode"
TIMEOUT_SEC = 15


class BillingError(RuntimeError):
    """Upstream billing fetch failed."""


def _headers(auth: GrokAuth) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {auth.key}",
        "X-XAI-Token-Auth": TOKEN_AUTH_HEADER_VALUE,
        "x-userid": auth.user_id,
        "x-grok-client-version": client_version(),
        CLIENT_MODE_HEADER: "interactive",
        "Accept": "application/json",
    }


def fetch_billing_raw(
    auth: Optional[GrokAuth] = None,
    *,
    proxy_base: str = DEFAULT_PROXY_BASE,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    auth = auth or load_auth()
    base = proxy_base.rstrip("/")
    url = f"{base}/billing?format=credits"
    http = session or requests.Session()
    try:
        resp = http.get(url, headers=_headers(auth), timeout=TIMEOUT_SEC)
    except requests.RequestException as exc:
        raise BillingError(f"Billing request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        raise BillingError("Auth rejected — open Grok Build and run /login.")
    if not resp.ok:
        detail = _error_detail(resp)
        raise BillingError(f"Billing service error: {detail}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise BillingError("Billing response was not JSON.") from exc
    if not isinstance(data, dict):
        raise BillingError("Unexpected billing payload shape.")
    return data


def fetch_auto_topup_raw(
    auth: Optional[GrokAuth] = None,
    *,
    proxy_base: str = DEFAULT_PROXY_BASE,
    session: Optional[requests.Session] = None,
) -> Optional[dict[str, Any]]:
    auth = auth or load_auth()
    base = proxy_base.rstrip("/")
    url = f"{base}/auto-topup-rule"
    http = session or requests.Session()
    try:
        resp = http.get(url, headers=_headers(auth), timeout=min(TIMEOUT_SEC, 10))
    except requests.RequestException as exc:
        log.debug("auto-topup fetch failed: %s", type(exc).__name__)
        return None
    if not resp.ok:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def fetch_snapshot(
    *,
    proxy_base: str = DEFAULT_PROXY_BASE,
    include_auto_topup: bool = True,
) -> UsageSnapshot:
    """High-level fetch used by the menu bar app."""
    try:
        auth = load_auth()
    except AuthError as exc:
        return snapshot_from_billing({}, error=str(exc))

    try:
        billing = fetch_billing_raw(auth, proxy_base=proxy_base)
    except BillingError as exc:
        return snapshot_from_billing({}, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any surprise as HUD text
        log.exception("unexpected billing failure")
        return snapshot_from_billing({}, error=f"Unexpected error: {type(exc).__name__}")

    auto = None
    if include_auto_topup:
        auto = fetch_auto_topup_raw(auth, proxy_base=proxy_base)

    return snapshot_from_billing(billing, auto_topup=auto)


def _error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, str) and err:
                return err
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
    except ValueError:
        pass
    return f"HTTP {resp.status_code}"
