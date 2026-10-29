from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    ProviderTokenVerifier,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.testclient import TestClient

from src.brain.connector.auth.login import login_routes
from src.brain.connector.auth.provider import VaultOAuthProvider, set_password, totp_setup
from src.brain.connector.auth.store import MAX_IDLE_CLIENTS, AuthStore
from src.brain.connector.auth.throttle import Throttle
from src.brain.connector.config import DEFAULT_REDIRECT_URIS, ConnectorConfig

PUBLIC_URL = "https://vault.example.com"
FULL = f"{PUBLIC_URL}/mcp"
LIMITED = f"{PUBLIC_URL}/mcp/limited"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "a long password for the vault connector"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _config(
    tmp_path: Path, redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
) -> ConnectorConfig:
    return ConnectorConfig(
        vault_path=tmp_path / "vault",
        data_dir=tmp_path,
        public_url=PUBLIC_URL,
        allowed_redirect_uris=redirect_uris,
    )


def _setup(tmp_path: Path, clock: Clock | None = None) -> tuple[AuthStore, VaultOAuthProvider]:
    db_path = tmp_path / "auth.db"
    store = AuthStore(db_path) if clock is None else AuthStore(db_path, clock=clock)
    return store, VaultOAuthProvider(_config(tmp_path), store)


def _client(client_id: str = "client-1", *redirect_uris: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secrets.token_hex(16),
        redirect_uris=[AnyUrl(uri) for uri in redirect_uris or (CALLBACK,)],
        token_endpoint_auth_method="client_secret_post",
    )


def _registered(
    provider: VaultOAuthProvider, client_id: str = "client-1"
) -> OAuthClientInformationFull:
    client = _client(client_id)
    asyncio.run(provider.register_client(client))
    return client


def _params(resource: str | None) -> AuthorizationParams:
    return AuthorizationParams(
        state="state-1",
        scopes=None,
        code_challenge="c" * 43,
        redirect_uri=AnyUrl(CALLBACK),
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _code_from(provider: VaultOAuthProvider, pending_id: str) -> str:
    redirect = provider.issue_code(pending_id)
    assert redirect is not None
    return _query(redirect)["code"]


def _code(
    provider: VaultOAuthProvider, client: OAuthClientInformationFull, resource: str = FULL
) -> str:
    login = asyncio.run(provider.authorize(client, _params(resource)))
    return _code_from(provider, _query(login)["pending"])


def _tokens(
    provider: VaultOAuthProvider, client: OAuthClientInformationFull, resource: str = FULL
) -> OAuthToken:
    code = asyncio.run(provider.load_authorization_code(client, _code(provider, client, resource)))
    assert code is not None
    return asyncio.run(provider.exchange_authorization_code(client, code))


def _access(provider: VaultOAuthProvider, token: str) -> AccessToken | None:
    return asyncio.run(provider.load_access_token(token))


def _refresh(
    provider: VaultOAuthProvider, client: OAuthClientInformationFull, token: str | None
) -> RefreshToken | None:
    assert token is not None
    return asyncio.run(provider.load_refresh_token(client, token))


def _rows(db_path: Path, table: str) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_auth_db_is_private_with_its_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    old_umask = os.umask(0)
    try:
        AuthStore(db_path)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    with closing(sqlite3.connect(db_path)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"clients", "pending", "codes", "tokens", "settings"} <= tables

    os.chmod(db_path, 0o644)
    AuthStore(db_path)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_register_foreign_redirect_refused(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    for client in (
        _client("foreign", "https://evil.example/callback"),
        _client("mixed", CALLBACK, "https://evil.example/callback"),
        _client("lookalike", "https://claude.ai.evil.example/api/mcp/auth_callback"),
        _client("trailing-slash", CALLBACK + "/"),
        _client("other-path", "https://claude.ai/api/mcp/other_callback"),
        OAuthClientInformationFull(client_id="none", redirect_uris=None),
    ):
        with pytest.raises(RegistrationError) as refused:
            asyncio.run(provider.register_client(client))
        assert refused.value.error == "invalid_redirect_uri"
        assert asyncio.run(provider.get_client(client.client_id)) is None


def test_register_claude_redirect_ok(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _client("claude", *DEFAULT_REDIRECT_URIS)
    asyncio.run(provider.register_client(client))
    assert asyncio.run(provider.get_client("claude")) == client
    assert asyncio.run(provider.get_client("unknown")) is None


def test_code_requires_pkce(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.db")
    provider = VaultOAuthProvider(_config(tmp_path), store)
    set_password(store, PASSWORD)
    totp = pyotp.TOTP(_query(totp_setup(store))["secret"])
    app = Starlette(
        routes=[
            *create_auth_routes(
                provider,
                AnyHttpUrl(PUBLIC_URL),
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
            ),
            *login_routes(provider, store, Throttle(alert=lambda message: None)),
        ]
    )
    http = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)
    registered = http.post("/register", json={"redirect_uris": [CALLBACK]})
    assert registered.status_code == 201
    client_id, client_secret = registered.json()["client_id"], registered.json()["client_secret"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    authorize = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "state": "state-1",
        "resource": FULL,
    }

    for refused_query in (
        authorize,
        {**authorize, "code_challenge": verifier, "code_challenge_method": "plain"},
    ):
        refused = http.get("/authorize", params=refused_query)
        assert refused.status_code == 302
        assert refused.headers["location"].startswith(CALLBACK + "?")
        assert _query(refused.headers["location"])["error"] == "invalid_request"
        assert "code" not in _query(refused.headers["location"])
    assert _rows(tmp_path / "auth.db", "pending") == 0

    started = http.get(
        "/authorize",
        params={**authorize, "code_challenge": challenge, "code_challenge_method": "S256"},
    )
    assert started.status_code == 302
    assert started.headers["location"].startswith(f"{PUBLIC_URL}/login?pending=")
    pending = _query(started.headers["location"])["pending"]
    signed_in = http.post(
        "/login", data={"pending": pending, "password": PASSWORD, "totp": totp.now()}
    )
    assert signed_in.status_code == 302
    code = _query(signed_in.headers["location"])["code"]
    exchange = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    no_verifier = http.post("/token", data=exchange)
    assert no_verifier.status_code == 400
    assert no_verifier.json()["error"] == "invalid_request"
    wrong_verifier = http.post(
        "/token", data={**exchange, "code_verifier": secrets.token_urlsafe(48)}
    )
    assert wrong_verifier.status_code == 400
    assert wrong_verifier.json()["error"] == "invalid_grant"

    granted = http.post("/token", data={**exchange, "code_verifier": verifier})
    assert granted.status_code == 200
    access = _access(provider, granted.json()["access_token"])
    assert access is not None and access.resource == FULL


def test_token_bound_to_resource(tmp_path: Path) -> None:
    store, provider = _setup(tmp_path)
    client = _registered(provider)
    limited = _tokens(provider, client, LIMITED)

    access = _access(provider, limited.access_token)
    assert access is not None and access.resource == LIMITED
    refresh = _refresh(provider, client, limited.refresh_token)
    assert refresh is not None and refresh.resource == LIMITED

    def admitted(endpoint: str) -> bool:
        backend = BearerAuthBackend(
            ProviderTokenVerifier(provider), resource_server_url=AnyHttpUrl(endpoint)
        )
        request = HTTPConnection(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {limited.access_token}".encode())],
            }
        )
        return asyncio.run(backend.authenticate(request)) is not None

    assert admitted(LIMITED)
    assert not admitted(FULL)

    for resource in (f"{PUBLIC_URL}/other", "https://evil.example/mcp", None):
        pending = store.add_pending(client.client_id, _params(resource).model_dump_json())
        code = asyncio.run(provider.load_authorization_code(client, _code_from(provider, pending)))
        assert code is not None
        with pytest.raises(TokenError) as refused:
            asyncio.run(provider.exchange_authorization_code(client, code))
        assert refused.value.error == "invalid_target"


def test_authorize_refuses_unknown_resource_and_redirect(tmp_path: Path) -> None:
    store, provider = _setup(tmp_path)
    client = _registered(provider)
    for resource in (
        None,
        f"{PUBLIC_URL}/other",
        f"{PUBLIC_URL}/mcp/limited/more",
        "https://evil.example/mcp",
    ):
        with pytest.raises(AuthorizeError) as refused:
            asyncio.run(provider.authorize(client, _params(resource)))
        assert refused.value.error == "invalid_target"
    assert _rows(tmp_path / "auth.db", "pending") == 0

    spelled = _tokens(provider, client, "https://VAULT.example.com/mcp/limited/")
    access = _access(provider, spelled.access_token)
    assert access is not None and access.resource == LIMITED

    narrowed = VaultOAuthProvider(
        _config(tmp_path, ("https://claude.com/api/mcp/auth_callback",)), store
    )
    with pytest.raises(AuthorizeError) as refused:
        asyncio.run(narrowed.authorize(client, _params(FULL)))
    assert refused.value.error == "invalid_request"


def test_register_refuses_large_clients_and_keeps_unique_redirects(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    large = _client("large").model_copy(update={"client_name": "x" * 9000})
    with pytest.raises(RegistrationError) as refused:
        asyncio.run(provider.register_client(large))
    assert refused.value.error == "invalid_client_metadata"
    assert asyncio.run(provider.get_client("large")) is None

    named = _client("named").model_copy(update={"client_name": "x" * 7000})
    asyncio.run(provider.register_client(named))
    assert asyncio.run(provider.get_client("named")) == named

    repeated = _client("repeated", *DEFAULT_REDIRECT_URIS * 5000)
    asyncio.run(provider.register_client(repeated))
    stored = asyncio.run(provider.get_client("repeated"))
    assert stored is not None
    assert [str(uri) for uri in stored.redirect_uris or []] == list(DEFAULT_REDIRECT_URIS)
    assert (tmp_path / "auth.db").stat().st_size < 128 * 1024


def test_authorize_refuses_odd_code_challenge_and_long_state(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _registered(provider)
    usual = _params(FULL)
    for challenge in ("c" * 42, "c" * 44, "c" * 42 + "+", "c" * 42 + "=", "c" * 42 + "/"):
        with pytest.raises(AuthorizeError) as refused:
            asyncio.run(
                provider.authorize(client, usual.model_copy(update={"code_challenge": challenge}))
            )
        assert refused.value.error == "invalid_request"
    for state in ("s" * 1025, "é" * 513):
        with pytest.raises(AuthorizeError) as refused:
            asyncio.run(provider.authorize(client, usual.model_copy(update={"state": state})))
        assert refused.value.error == "invalid_request"
    assert _rows(tmp_path / "auth.db", "pending") == 0

    edge = usual.model_copy(
        update={
            "state": "s" * 1024,
            "code_challenge": "Az09-_" + "c" * 37,
            "scopes": ["read"] * 10_000,
        }
    )
    login = asyncio.run(provider.authorize(client, edge))
    code = asyncio.run(
        provider.load_authorization_code(client, _code_from(provider, _query(login)["pending"]))
    )
    assert code is not None and code.scopes == ["read"]


def test_refresh_rotates_and_old_refresh_dies(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _registered(provider)
    first = _tokens(provider, client, LIMITED)
    old = _refresh(provider, client, first.refresh_token)
    assert old is not None

    second = asyncio.run(provider.exchange_refresh_token(client, old, old.scopes))
    assert second.refresh_token is not None
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token
    assert second.expires_in == 3600

    assert _refresh(provider, client, first.refresh_token) is None
    with pytest.raises(TokenError) as reused:
        asyncio.run(provider.exchange_refresh_token(client, old, old.scopes))
    assert reused.value.error == "invalid_grant"
    assert _access(provider, first.access_token) is None

    new_access = _access(provider, second.access_token)
    assert new_access is not None and new_access.resource == LIMITED
    new_refresh = _refresh(provider, client, second.refresh_token)
    assert new_refresh is not None and new_refresh.resource == LIMITED
    third = asyncio.run(provider.exchange_refresh_token(client, new_refresh, new_refresh.scopes))
    assert _access(provider, third.access_token) is not None


def test_refresh_keeps_its_scopes_when_access_narrows(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _registered(provider)
    params = _params(FULL).model_copy(update={"scopes": ["read", "write"]})
    login = asyncio.run(provider.authorize(client, params))
    code = asyncio.run(
        provider.load_authorization_code(client, _code_from(provider, _query(login)["pending"]))
    )
    assert code is not None
    first = asyncio.run(provider.exchange_authorization_code(client, code))
    assert first.scope == "read write"
    refresh = _refresh(provider, client, first.refresh_token)
    assert refresh is not None

    with pytest.raises(TokenError) as refused:
        asyncio.run(provider.exchange_refresh_token(client, refresh, ["admin"]))
    assert refused.value.error == "invalid_scope"
    narrowed = asyncio.run(provider.exchange_refresh_token(client, refresh, ["read"]))
    access = _access(provider, narrowed.access_token)
    assert access is not None and access.scopes == ["read"]
    kept = _refresh(provider, client, narrowed.refresh_token)
    assert kept is not None and kept.scopes == ["read", "write"]


def test_tokens_belong_to_their_client_and_kind(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _registered(provider)
    other = _registered(provider, "client-2")
    tokens = _tokens(provider, client)

    assert _refresh(provider, other, tokens.refresh_token) is None
    assert _refresh(provider, client, tokens.access_token) is None
    assert _access(provider, tokens.refresh_token or "") is None
    code = _code(provider, client)
    assert asyncio.run(provider.load_authorization_code(other, code)) is None


def test_access_token_expires_after_1h(tmp_path: Path) -> None:
    clock = Clock()
    _, provider = _setup(tmp_path, clock)
    client = _registered(provider)
    issued = clock.now
    tokens = _tokens(provider, client)
    assert tokens.expires_in == 3600

    access = _access(provider, tokens.access_token)
    assert access is not None
    assert access.expires_at == int((issued + timedelta(hours=1)).timestamp())
    clock.now = issued + timedelta(minutes=59, seconds=59)
    assert _access(provider, tokens.access_token) is not None
    clock.now = issued + timedelta(hours=1)
    assert _access(provider, tokens.access_token) is None

    clock.now = issued + timedelta(days=30, seconds=-1)
    assert _refresh(provider, client, tokens.refresh_token) is not None
    clock.now = issued + timedelta(days=30)
    assert _refresh(provider, client, tokens.refresh_token) is None


def test_code_is_single_use_and_lasts_10_minutes(tmp_path: Path) -> None:
    clock = Clock()
    _, provider = _setup(tmp_path, clock)
    client = _registered(provider)

    code = asyncio.run(provider.load_authorization_code(client, _code(provider, client)))
    assert code is not None
    assert code.expires_at == (clock.now + timedelta(minutes=10)).timestamp()
    assert (code.client_id, code.resource, str(code.redirect_uri)) == (
        client.client_id,
        FULL,
        CALLBACK,
    )
    asyncio.run(provider.exchange_authorization_code(client, code))
    assert asyncio.run(provider.load_authorization_code(client, code.code)) is None
    with pytest.raises(TokenError) as reused:
        asyncio.run(provider.exchange_authorization_code(client, code))
    assert reused.value.error == "invalid_grant"

    late = _code(provider, client)
    clock.now += timedelta(minutes=10)
    assert asyncio.run(provider.load_authorization_code(client, late)) is None


def test_revoke_token_revokes_its_sign_in(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _registered(provider)
    one = _tokens(provider, client)
    two = _tokens(provider, client, LIMITED)

    access = _access(provider, one.access_token)
    assert access is not None
    asyncio.run(provider.revoke_token(access))
    assert _access(provider, one.access_token) is None
    assert _refresh(provider, client, one.refresh_token) is None
    assert _access(provider, two.access_token) is not None

    refresh = _refresh(provider, client, two.refresh_token)
    assert refresh is not None
    asyncio.run(provider.revoke_token(refresh))
    assert _access(provider, two.access_token) is None
    assert _refresh(provider, client, two.refresh_token) is None


def test_revoke_all(tmp_path: Path) -> None:
    store, provider = _setup(tmp_path)
    client = _registered(provider)
    one = _tokens(provider, client)
    two = _tokens(provider, client, LIMITED)
    code = _code(provider, client)
    pending = _query(asyncio.run(provider.authorize(client, _params(FULL))))["pending"]

    assert store.revoke_all() == 5

    for tokens in (one, two):
        assert _access(provider, tokens.access_token) is None
        assert _refresh(provider, client, tokens.refresh_token) is None
    assert asyncio.run(provider.load_authorization_code(client, code)) is None
    assert provider.issue_code(pending) is None
    assert asyncio.run(provider.get_client(client.client_id)) == client
    assert _access(provider, _tokens(provider, client).access_token) is not None


def test_tokens_stored_hashed(tmp_path: Path) -> None:
    _, provider = _setup(tmp_path)
    client = _registered(provider)
    tokens = _tokens(provider, client)
    unused_code = _code(provider, client)
    assert tokens.refresh_token is not None

    stored = (tmp_path / "auth.db").read_bytes()
    for raw in (tokens.access_token, tokens.refresh_token, unused_code):
        assert raw.encode() not in stored
    for raw in (tokens.access_token, tokens.refresh_token, unused_code):
        assert hashlib.sha256(raw.encode()).hexdigest().encode() in stored


def test_set_password_and_totp_setup(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    store = AuthStore(db_path)
    set_password(store, PASSWORD)
    with closing(sqlite3.connect(db_path)) as conn:
        values = [row[0] for row in conn.execute("SELECT value FROM settings")]
    assert len(values) == 1 and values[0].startswith("$argon2id$")
    assert PASSWORD.encode() not in db_path.read_bytes()
    with pytest.raises(ValueError):
        set_password(store, "")

    uri = totp_setup(store)
    secret = _query(uri)["secret"]
    assert uri == f"otpauth://totp/Vault:owner?secret={secret}&issuer=Vault"
    assert len(base64.b32decode(secret)) == 20
    assert totp_setup(store) != uri


def test_idle_client_registrations_are_capped(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    store = AuthStore(db_path)
    store.add_client("live", "{}")
    store.add_tokens("live", [], "https://vault.example.com/mcp", "access", "refresh")
    for n in range(MAX_IDLE_CLIENTS + 5):
        store.add_client(f"idle-{n}", "{}")
    assert store.get_client("live") is not None
    assert store.get_client("idle-0") is None
    assert store.get_client("idle-4") is None
    assert store.get_client("idle-5") is not None
    assert store.get_client(f"idle-{MAX_IDLE_CLIENTS + 4}") is not None
    assert _rows(db_path, "clients") == MAX_IDLE_CLIENTS + 1
