"""Turning tokens into identities.

Production identities arrive from CDX and are deliberately not signature-checked
here; see decode_cdx_token for why. Development identities are minted and
verified by this server and are fully validated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, FrozenSet

import jwt


PROJECT_ADMIN_ROLE = "PROJECT_ADMIN"
CDX_IDENTITY_NAMESPACE = "cdx"
CDX_ROLE_CLAIM = "roles"
DEVELOPMENT_TOKEN_ISSUER = "deep-agents-development-login"


class AuthenticationError(ValueError):
    """The caller could not be identified from the request."""

    pass


@dataclass(frozen=True)
class TokenIdentity:
    """Who the caller is, as asserted by whichever issuer vouched for them."""

    issuer: str
    subject: str
    roles: FrozenSet[str]
    claims: dict[str, Any]

    @property
    def is_project_admin(self) -> bool:
        """Whether this identity carries the one role the app authorizes on."""
        return PROJECT_ADMIN_ROLE in self.roles


def _roles_from_claim(value: Any) -> FrozenSet[str]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value]
    else:
        values = []
    return frozenset(role.strip() for role in values if role.strip())


def decode_cdx_token(token: str) -> TokenIdentity:
    """Decode identity claims from a token already authenticated by CDX.

    Signature, issuer, audience, and lifetime validation deliberately belong to
    CDX. In production, this application must only be reachable through CDX,
    which overwrites the x-fnma-jws-token request header.
    """
    if not token or not token.strip():
        raise AuthenticationError("Missing x-fnma-jws-token header")

    try:
        claims = jwt.decode(
            token.strip(),
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iss": False,
                "verify_aud": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Malformed x-fnma-jws-token header") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticationError("JWT subject is missing or invalid")

    return TokenIdentity(
        issuer=CDX_IDENTITY_NAMESPACE,
        subject=subject.strip(),
        roles=_roles_from_claim(claims.get(CDX_ROLE_CLAIM)),
        claims=dict(claims),
    )


def create_development_token(
    subject: str,
    roles: list[str],
    signing_secret: str,
    lifetime_seconds: int,
) -> str:
    """Mint a short-lived local identity for the development login."""
    now = int(time.time())
    return jwt.encode(
        {
            "iss": DEVELOPMENT_TOKEN_ISSUER,
            "sub": subject,
            CDX_ROLE_CLAIM: roles,
            "iat": now,
            "exp": now + lifetime_seconds,
        },
        signing_secret,
        algorithm="HS256",
    )


def decode_development_token(token: str, signing_secret: str) -> TokenIdentity:
    """Verify a locally issued development identity.

    Unlike the CDX path this does check the signature, because the server itself
    minted the token and nothing upstream has vouched for it.
    """
    try:
        claims = jwt.decode(
            token,
            signing_secret,
            algorithms=["HS256"],
            issuer=DEVELOPMENT_TOKEN_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Development identity is invalid or expired") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticationError("Development identity subject is missing or invalid")

    return TokenIdentity(
        issuer=CDX_IDENTITY_NAMESPACE,
        subject=subject.strip(),
        roles=_roles_from_claim(claims.get(CDX_ROLE_CLAIM)),
        claims=dict(claims),
    )
