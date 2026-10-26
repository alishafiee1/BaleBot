import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"v1"
NONCE_LEN = 12


class VaultError(Exception):
    """Wrong master key, tampered blob, or a blob bound to another owner."""


class Vault:
    """AES-256-GCM. The AAD ties every blob to its owner, so a copied row never decrypts."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("vault key must be 32 bytes")
        self._aead = AESGCM(master_key)

    @classmethod
    def from_b64(cls, value: str) -> "Vault":
        try:
            return cls(base64.urlsafe_b64decode(value.encode()))
        except ValueError as exc:
            raise ValueError("vault key must be urlsafe-base64 of 32 bytes") from exc

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode()

    def encrypt(self, plaintext: str, aad: str) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        return VERSION + nonce + self._aead.encrypt(nonce, plaintext.encode(), aad.encode())

    def decrypt(self, blob: bytes, aad: str) -> str:
        if not blob.startswith(VERSION) or len(blob) < len(VERSION) + NONCE_LEN + 16:
            raise VaultError("bad blob")
        nonce = blob[len(VERSION) : len(VERSION) + NONCE_LEN]
        try:
            return self._aead.decrypt(
                nonce, blob[len(VERSION) + NONCE_LEN :], aad.encode()
            ).decode()
        except InvalidTag:
            raise VaultError("decryption failed") from None


def key_aad(user_id: int, key_id: int) -> str:
    return f"userkey:{user_id}:{key_id}"
