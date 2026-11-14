"""Encryption of secrets at rest (invariant I7).

AES-256-GCM. Ciphertext format: ``<key_id>:<base64url(nonce || ciphertext)>``.
The first configured key encrypts; every configured key can decrypt, which allows rotation.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REDACTED = "••••••"


class CryptoError(Exception):
    pass


def _decode_key(raw: str) -> bytes:
    raw = raw.strip()
    padded = raw + "=" * (-len(raw) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(padded)
        except ValueError:
            continue
        if len(key) == 32:
            return key
    raise CryptoError("Each secret key must be 32 bytes encoded in base64.")


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


class SecretBox:
    def __init__(self, keys_spec: str) -> None:
        self._keys: dict[str, AESGCM] = {}
        self._primary: str | None = None
        for index, part in enumerate(p for p in keys_spec.split(",") if p.strip()):
            key_id, _, encoded = part.strip().rpartition(":")
            key_id = key_id or f"k{index + 1}"
            if key_id in self._keys:
                raise CryptoError(f"Duplicate secret key id {key_id!r}.")
            self._keys[key_id] = AESGCM(_decode_key(encoded))
            if self._primary is None:
                self._primary = key_id

    @property
    def configured(self) -> bool:
        return self._primary is not None

    def encrypt(self, plaintext: str, *, context: str = "") -> str:
        if self._primary is None:
            raise CryptoError("No secret key configured (ZP_SECRET_KEYS).")
        nonce = os.urandom(12)
        sealed = self._keys[self._primary].encrypt(nonce, plaintext.encode(), context.encode())
        return f"{self._primary}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"

    def decrypt(self, token: str, *, context: str = "") -> str:
        key_id, sep, payload = token.partition(":")
        if not sep or key_id not in self._keys:
            raise CryptoError("Unknown secret key id; was the key removed from ZP_SECRET_KEYS?")
        blob = base64.urlsafe_b64decode(payload)
        try:
            plain = self._keys[key_id].decrypt(blob[:12], blob[12:], context.encode())
        except Exception as exc:  # InvalidTag
            raise CryptoError("Secret could not be decrypted (wrong key or tampered).") from exc
        return plain.decode()

    def encrypt_json(self, value: Any, *, context: str = "") -> str:
        return self.encrypt(json.dumps(value, separators=(",", ":")), context=context)

    def decrypt_json(self, token: str, *, context: str = "") -> Any:
        return json.loads(self.decrypt(token, context=context))


def mask(value: str, visible: int = 4) -> str:
    """Show only the last characters of a secret, e.g. for ``api key ••••••a1b2``."""
    if len(value) <= visible * 2:
        return REDACTED
    return f"{REDACTED}{value[-visible:]}"
