"""The vault connector's OAuth provider, on the ``mcp`` library's interfaces.

claude.ai registers itself as a client, sends the owner's browser to the
authorization endpoint and gets a code back at its callback, which it
exchanges for tokens. The library's routes handle the protocol, including the
PKCE check; this provider decides who may register, what each code and token
is good for and for how long, and checks the owner's password and one-time
code for the sign-in page (``login.py``).

- A client may register only when every one of its redirect addresses is in
  ``config.allowed_redirect_uris``, and a sign-in is started only for such an
  address, so a code is only ever sent to claude.ai.
- Each sign-in is for one resource, ``<public_url>/mcp`` (full) or
  ``<public_url>/mcp/limited``. Its code and tokens record it, so a token for
  the limited endpoint is refused at the full one. Any other resource, or
  none, is refused.
- Codes last 10 minutes and work once. Access tokens last 1 hour and refresh
  tokens 30 days. Using a refresh token replaces both tokens of its sign-in.
  Revoking a token revokes both tokens of its sign-in.
- What strangers can store is bounded: a client's stored registration is at
  most 8 KiB with each redirect address once; a pending sign-in keeps a
  43-character PKCE challenge, a ``state`` of at most 1 KiB and each scope
  once.
- The password is kept as an argon2 hash and the one-time code secret as
  text, both in ``auth.db``. A code is checked only after the password, and
  each code signs in once.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode, urlparse

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl, ValidationError

from src.brain.connector.auth.store import (
    ACCESS_TTL,
    HASH_SETTING,
    TOTP_SETTING,
    AuthStore,
)
from src.brain.connector.config import ConnectorConfig

OWNER = "owner"
_CODE_DIGITS = 6
_MAX_CLIENT_BYTES = 8 * 1024
_MAX_STATE_BYTES = 1024
_S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")

CheckResult = Literal["right", "wrong password", "wrong code", "code used"]


@dataclass(frozen=True)
class SignInRequest:
    """What the sign-in page shows: the site the code goes back to, and the endpoint asked for."""

    host: str
    access: Literal["full", "limited"]


def _canonical(url: str) -> str | None:
    """``url`` spelled the way the library compares resources, or None if it is not an http(s) URL."""
    try:
        return str(AnyHttpUrl(url)).removesuffix("/")
    except ValidationError:
        return None


def _token(access: str, refresh: str, scopes: list[str]) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        expires_in=int(ACCESS_TTL.total_seconds()),
        scope=" ".join(scopes) or None,
        refresh_token=refresh,
    )


class VaultOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, config: ConnectorConfig, store: AuthStore) -> None:
        base = config.public_url.rstrip("/")
        full = _canonical(f"{base}/mcp")
        limited = _canonical(f"{base}/mcp/limited")
        if full is None or limited is None:
            raise ValueError(f"public_url is not an http(s) address: {config.public_url!r}")
        self._store = store
        self._full = full
        self._resources = (full, limited)
        self._login_url = f"{base}/login"
        self._redirect_uris = frozenset(uri.strip() for uri in config.allowed_redirect_uris)

    def _resource(self, requested: str | None) -> str | None:
        """The endpoint ``requested`` names, spelled as stored, or None if it names neither."""
        canonical = None if requested is None else _canonical(requested)
        return canonical if canonical in self._resources else None

    def _resource_refused(self) -> str:
        return f"resource must be {self._resources[0]} or {self._resources[1]}"

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self._store.get_client(client_id)
        return None if info is None else OAuthClientInformationFull.model_validate_json(info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = list(dict.fromkeys(str(uri).strip() for uri in client_info.redirect_uris or ()))
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "a redirect address is required")
        refused = [uri for uri in uris if uri not in self._redirect_uris]
        if refused:
            raise RegistrationError(
                "invalid_redirect_uri", f"redirect address not allowed: {refused[0]}"
            )
        stored = client_info.model_copy(update={"redirect_uris": [AnyUrl(uri) for uri in uris]})
        info = stored.model_dump_json()
        if len(info.encode()) > _MAX_CLIENT_BYTES:
            raise RegistrationError(
                "invalid_client_metadata", f"client metadata is over {_MAX_CLIENT_BYTES} bytes"
            )
        self._store.add_client(client_info.client_id, info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if str(params.redirect_uri).strip() not in self._redirect_uris:
            raise AuthorizeError("invalid_request", "redirect address not allowed")
        resource = self._resource(params.resource)
        if resource is None:
            raise AuthorizeError("invalid_target", self._resource_refused())
        if not _S256_CHALLENGE.fullmatch(params.code_challenge):
            raise AuthorizeError(
                "invalid_request", "code_challenge must be 43 base64url characters"
            )
        if params.state is not None and len(params.state.encode()) > _MAX_STATE_BYTES:
            raise AuthorizeError("invalid_request", f"state is over {_MAX_STATE_BYTES} bytes")
        scopes = None if params.scopes is None else list(dict.fromkeys(params.scopes))
        stored = params.model_copy(update={"resource": resource, "scopes": scopes})
        pending_id = self._store.add_pending(client.client_id, stored.model_dump_json())
        return f"{self._login_url}?{urlencode({'pending': pending_id})}"

    def sign_in_request(self, pending_id: str) -> SignInRequest | None:
        """What a pending sign-in asks for, or None if it is unknown, expired or used."""
        row = self._store.get_pending(pending_id)
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row.params)
        return SignInRequest(
            host=urlparse(str(params.redirect_uri)).hostname or "",
            access="full" if params.resource == self._full else "limited",
        )

    def issue_code(self, pending_id: str) -> str | None:
        """Turn a pending sign-in into a code and return the address to send the browser to.

        Call it only once the owner's password and one-time code have been
        checked. Returns None when the pending sign-in is unknown, expired or
        already used.
        """
        code = secrets.token_urlsafe(32)
        row = self._store.pending_to_code(pending_id, code)
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row.params)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._store.get_code(authorization_code, client.client_id)
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row.params)
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [],
            expires_at=row.expires_at,
            client_id=row.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=OWNER,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        row = self._store.take_code(authorization_code.code, client.client_id)
        if row is None:
            raise TokenError("invalid_grant", "authorization code is not valid")
        params = AuthorizationParams.model_validate_json(row.params)
        resource = self._resource(params.resource)
        if resource is None:
            raise TokenError("invalid_target", self._resource_refused())
        scopes = params.scopes or []
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._store.add_tokens(client.client_id, scopes, resource, access, refresh)
        return _token(access, refresh, scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._store.get_token(refresh_token, "refresh")
        if row is None or row.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=list(row.scopes),
            expires_at=row.expires_at,
            resource=row.resource,
            subject=OWNER,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not set(scopes) <= set(refresh_token.scopes):
            raise TokenError(
                "invalid_scope", "a scope was asked for that the refresh token does not have"
            )
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if not self._store.rotate_refresh(
            refresh_token.token, client.client_id, scopes, access, refresh
        ):
            raise TokenError("invalid_grant", "refresh token is not valid")
        return _token(access, refresh, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._store.get_token(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=list(row.scopes),
            expires_at=row.expires_at,
            resource=row.resource,
            subject=OWNER,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._store.revoke(token.token)


def set_password(store: AuthStore, password: str) -> None:
    """Keep an argon2 hash of ``password`` as the connector's sign-in password."""
    if not password:
        raise ValueError("the password must not be empty")
    store.set_setting(HASH_SETTING, PasswordHasher().hash(password))


def totp_setup(store: AuthStore) -> str:
    """Make and keep a new one-time code secret; return the ``otpauth://`` address for an app.

    The address holds the secret: show it to the owner once, never log it.
    A new secret replaces the old one, whose codes stop working.
    """
    secret = pyotp.random_base32()
    store.set_setting(TOTP_SETTING, secret)
    return pyotp.TOTP(secret).provisioning_uri(name=OWNER, issuer_name="Vault")


def sign_in_set_up(store: AuthStore) -> bool:
    """True when both a password and a one-time code secret are set."""
    return store.get_setting(HASH_SETTING) is not None and store.get_setting(TOTP_SETTING) is not None


def _code_step(totp: pyotp.TOTP, code: str, now: datetime) -> int | None:
    """The time step ``code`` belongs to, within one step either side of ``now``, or None."""
    if len(code) != _CODE_DIGITS or not code.isascii() or not code.isdigit():
        return None
    if not totp.verify(code, for_time=now, valid_window=1):
        return None
    current = totp.timecode(now)
    return next(
        (
            step
            for step in (current - 1, current, current + 1)
            if secrets.compare_digest(totp.generate_otp(step), code)
        ),
        None,
    )


def check_password_and_code(store: AuthStore, password: str, code: str) -> CheckResult:
    """Check the owner's password and one-time code.

    Returns "right" when both are right and the code has not signed in
    before; "wrong password"; "wrong code" when the password is right but the
    code is not one for this time; or "code used" when both are right but the
    code already signed in. The code is looked at only when the password is
    right, so a wrong password never uses up a code. Runs argon2, so call it
    off the event loop.
    """
    password_hash = store.get_setting(HASH_SETTING)
    secret = store.get_setting(TOTP_SETTING)
    if password_hash is None or secret is None:
        return "wrong password"
    try:
        PasswordHasher().verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return "wrong password"
    step = _code_step(pyotp.TOTP(secret), "".join(code.split()), store.now())
    if step is None:
        return "wrong code"
    return "right" if store.claim_code_step(step) else "code used"
