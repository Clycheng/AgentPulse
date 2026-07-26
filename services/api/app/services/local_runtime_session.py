"""Encrypt a one-run model credential for the Local Worker.

The API decrypts a workspace BYOK key only long enough to seal it to the
worker's ephemeral X25519 public key.  The renderer never receives the key and
the profile filesystem never stores it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)


class LocalRuntimeSessionError(ValueError):
    pass


def _decode_public_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise LocalRuntimeSessionError("本机运行时公钥格式无效") from exc
    return decoded


def runtime_session_aad(
    *, workspace_id: str, user_id: str, device_id: str, run_id: str, expires_at: str
) -> bytes:
    return json.dumps(
        {
            "device_id": device_id,
            "expires_at": expires_at,
            "run_id": run_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def seal_runtime_credential(
    *,
    client_public_key: str,
    workspace_id: str,
    user_id: str,
    device_id: str,
    run_id: str,
    api_key: str,
    model: str,
    ttl_seconds: int,
) -> dict[str, str]:
    """Return an AES-GCM envelope which only the in-memory worker can open."""
    try:
        peer = load_der_public_key(_decode_public_key(client_public_key))
    except ValueError as exc:
        raise LocalRuntimeSessionError("本机运行时公钥无效") from exc
    if not isinstance(peer, X25519PublicKey):
        raise LocalRuntimeSessionError("本机运行时必须使用 X25519 公钥")
    private = X25519PrivateKey.generate()
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    aad = runtime_session_aad(
        workspace_id=workspace_id,
        user_id=user_id,
        device_id=device_id,
        run_id=run_id,
        expires_at=expires_at,
    )
    shared = private.exchange(peer)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(aad).digest(),
        info=b"agentpulse/local-runtime/v1",
    ).derive(shared)
    nonce = os.urandom(12)
    plaintext = json.dumps(
        {"DEEPSEEK_API_KEY": api_key, "model": f"deepseek/{model}"},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {
        "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
        "server_public_key": base64.b64encode(
            private.public_key().public_bytes(
                Encoding.DER,
                PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "expires_at": expires_at,
    }
