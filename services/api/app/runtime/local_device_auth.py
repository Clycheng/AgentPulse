"""Short-lived, device-bound credentials for the desktop Local Worker."""

from __future__ import annotations

import hashlib
import secrets
import time

import jwt
from jwt.exceptions import InvalidTokenError

from app.core.config import settings


ALGORITHM = "HS256"
AUDIENCE = "agentpulse-local-worker"


def create_local_device_token(
    *, workspace_id: str, user_id: str, device_id: str
) -> tuple[str, int]:
    now = int(time.time())
    expires_at = now + settings.local_device_token_ttl_seconds
    token = jwt.encode(
        {
            "type": "local_device",
            "workspace_id": workspace_id,
            "user_id": user_id,
            "device_id": device_id,
            "iat": now,
            "exp": expires_at,
            "iss": "agentpulse-api",
            "aud": AUDIENCE,
            "jti": secrets.token_urlsafe(12),
        },
        settings.auth_secret_key,
        algorithm=ALGORITHM,
    )
    return token, expires_at


def decode_local_device_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.auth_secret_key,
            algorithms=[ALGORITHM],
            issuer="agentpulse-api",
            audience=AUDIENCE,
            options={
                "require": [
                    "type", "workspace_id", "user_id", "device_id",
                    "iat", "exp", "jti",
                ]
            },
        )
    except InvalidTokenError as exc:
        raise ValueError("invalid or expired local device token") from exc
    if payload.get("type") != "local_device":
        raise ValueError("invalid local device token type")
    return payload


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
