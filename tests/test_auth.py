"""OIDC session load and refresh. No real network and no real auth file."""

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from gbu.auth import AuthError, GrokAuth, access_expired, load_auth, refresh_auth
from gbu.billing import BillingError, _load_build


NOW = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http")

    def json(self):
        return self._payload


class _Session:
    def __init__(self, token_status=200):
        self.posts = []
        self.token_status = token_status

    def get(self, url, timeout=None):
        assert url == "https://auth.example/.well-known/openid-configuration"
        return _Response({"token_endpoint": "https://auth.example/token"})

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, dict(data)))
        if self.token_status != 200:
            return _Response({"error": "invalid_grant"}, status=self.token_status)
        return _Response(
            {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        )


def _write_auth(path, *, key="old-access", expires="2026-10-17T01:00:16Z"):
    path.write_text(
        json.dumps(
            {
                "legacy": {"key": "plain", "user_id": "u0"},
                "https://auth.example::client-1": {
                    "key": key,
                    "user_id": "user-1",
                    "team_id": "team-1",
                    "auth_mode": "oidc",
                    "expires_at": expires,
                    "refresh_token": "refresh-1",
                    "oidc_issuer": "https://auth.example",
                    "oidc_client_id": "client-1",
                    "principal_type": "User",
                    "principal_id": "user-1",
                    "email": "kept@example.com",
                },
            }
        ),
        encoding="utf-8",
    )


def test_load_auth_prefers_the_oidc_entry(tmp_path):
    path = tmp_path / "auth.json"
    _write_auth(path)
    auth = load_auth(path)
    assert auth.user_id == "user-1"
    assert auth.scope_key == "https://auth.example::client-1"
    assert auth.can_refresh
    assert auth.key == "old-access"


def test_access_expired_uses_the_skew_window():
    auth = GrokAuth("k", "u", None, "oidc", "2026-10-17T16:00:30Z")
    assert access_expired(auth, now=NOW) is True
    later = GrokAuth("k", "u", None, "oidc", "2026-10-17T18:00:00Z")
    assert access_expired(later, now=NOW) is False
    assert access_expired(GrokAuth("k", "u", None, "oidc", None), now=NOW) is False


def test_refresh_persists_only_token_fields(tmp_path):
    path = tmp_path / "auth.json"
    _write_auth(path)
    auth = load_auth(path)
    session = _Session()
    refreshed = refresh_auth(auth, path=path, session=session, now=NOW)
    assert refreshed.key == "new-access"
    assert refreshed.refresh_token == "new-refresh"
    assert refreshed.expires_at == "2026-10-17T17:00:00Z"
    assert session.posts[0][0] == "https://auth.example/token"
    assert session.posts[0][1] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "client-1",
        "principal_type": "User",
        "principal_id": "user-1",
    }
    stored = json.loads(path.read_text(encoding="utf-8"))
    entry = stored["https://auth.example::client-1"]
    assert entry["key"] == "new-access"
    assert entry["refresh_token"] == "new-refresh"
    assert entry["email"] == "kept@example.com"
    assert stored["legacy"]["key"] == "plain"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert not list(tmp_path.glob(".auth.json.*.tmp"))


def test_refresh_failure_leaves_the_file_alone(tmp_path):
    path = tmp_path / "auth.json"
    _write_auth(path)
    before = path.read_text(encoding="utf-8")
    auth = load_auth(path)
    with pytest.raises(AuthError, match="expired"):
        refresh_auth(auth, path=path, session=_Session(token_status=400), now=NOW)
    assert path.read_text(encoding="utf-8") == before


def test_expired_session_is_refreshed_before_billing(monkeypatch):
    stale = GrokAuth(
        "old",
        "user-1",
        None,
        "oidc",
        (NOW - timedelta(hours=1)).isoformat(),
        refresh_token="refresh-1",
        oidc_issuer="https://auth.example",
        oidc_client_id="client-1",
        scope_key="scope",
    )
    fresh = GrokAuth("new", "user-1", None, "oidc", "2026-10-17T18:00:00Z", scope_key="scope")
    calls = []

    monkeypatch.setattr("gbu.billing.load_auth", lambda: stale)
    monkeypatch.setattr("gbu.billing.access_expired", lambda auth: auth.key == "old")

    def refresh(auth, **_kwargs):
        calls.append("refresh")
        return fresh

    def fetch(auth, **_kwargs):
        calls.append(auth.key)
        return {"config": {"creditUsagePercent": 12}}

    monkeypatch.setattr("gbu.billing.refresh_auth", refresh)
    monkeypatch.setattr("gbu.billing.fetch_billing_raw", fetch)
    billing, _auto, error = _load_build(proxy_base="https://example", include_auto_topup=False)
    assert error is None
    assert billing["config"]["creditUsagePercent"] == 12
    assert calls == ["refresh", "new"]


def test_rejected_token_refreshes_once(monkeypatch):
    auth = GrokAuth(
        "old",
        "user-1",
        None,
        "oidc",
        "2026-10-17T18:00:00Z",
        refresh_token="refresh-1",
        oidc_issuer="https://auth.example",
        oidc_client_id="client-1",
        scope_key="scope",
    )
    fresh = GrokAuth("new", "user-1", None, "oidc", "2026-10-17T18:00:00Z")
    calls = []
    monkeypatch.setattr("gbu.billing.load_auth", lambda: auth)
    monkeypatch.setattr("gbu.billing.access_expired", lambda _auth: False)

    def refresh(_auth, **_kwargs):
        calls.append("refresh")
        return fresh

    def fetch(seen, **_kwargs):
        calls.append(seen.key)
        if seen.key == "old":
            raise BillingError("Auth rejected — open Grok Build and run /login.")
        return {"config": {"creditUsagePercent": 4}}

    monkeypatch.setattr("gbu.billing.refresh_auth", refresh)
    monkeypatch.setattr("gbu.billing.fetch_billing_raw", fetch)
    billing, _auto, error = _load_build(proxy_base="https://example", include_auto_topup=False)
    assert error is None
    assert calls == ["old", "refresh", "new"]
    assert billing["config"]["creditUsagePercent"] == 4
