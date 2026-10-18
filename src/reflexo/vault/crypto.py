"""Key rings and AES-256-GCM for the vault and the archive (BETA_10 §3.2, §4).

A key ring is `REFLEXO_VAULT_KEYS` = "<key_id>:<base64 32 bytes>[,<key_id>:<key>]" with one active id that encrypts
new data; every older id still decrypts until it is removed after a rewrap. Each ciphertext is bound to its owner by
the additional authenticated data (AAD), so a row copied to another business fails to decrypt.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


class KeyRingError(Exception):
    """A key ring that cannot be used (missing, malformed, wrong length, or an unknown key id)."""


class DecryptError(Exception):
    """The ciphertext does not open with this key and AAD (tampered, copied, or the wrong key)."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


class KeyRing:
    def __init__(self, spec: str, active_id: str) -> None:
        self._keys: dict[str, bytes] = {}
        for part in (p.strip() for p in spec.split(",") if p.strip()):
            key_id, _, encoded = part.partition(":")
            try:
                key = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise KeyRingError(f"key {key_id!r} is not base64") from exc
            if not key_id or len(key) != KEY_BYTES:
                raise KeyRingError(f"key {key_id!r} must be {KEY_BYTES} bytes")
            self._keys[key_id] = key
        if active_id not in self._keys:
            raise KeyRingError("the active key id is not in the key ring")
        self.active_id = active_id

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def raw(self) -> frozenset[bytes]:
        return frozenset(self._keys.values())

    def encrypt(self, plaintext: bytes, aad: bytes) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(self.active_id, nonce, AESGCM(self._keys[self.active_id]).encrypt(nonce, plaintext, aad))

    def decrypt(self, sealed: Sealed, aad: bytes) -> bytes:
        key = self._keys.get(sealed.key_id)
        if key is None:
            raise KeyRingError(f"key {sealed.key_id!r} is no longer in the key ring")
        try:
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad)
        except InvalidTag as exc:
            raise DecryptError("ciphertext does not open with this key and binding") from exc


def new_key() -> str:
    """A fresh base64 key (for `.env` set-up and tests)."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()
