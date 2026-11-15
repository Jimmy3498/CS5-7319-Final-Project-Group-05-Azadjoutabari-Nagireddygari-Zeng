from hashlib import scrypt
from hmac import compare_digest
from secrets import token_bytes

from ..models.common import SecretInput


class ScryptPasswordHasher:
    def _derive(self, secret: SecretInput, salt: bytes) -> bytes:
        return scrypt(
            secret.value.encode("utf-8"),
            salt=salt,
            n=32768,
            r=8,
            p=3,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    def hash(self, secret: SecretInput) -> str:
        salt = token_bytes(16)
        digest = self._derive(secret, salt)
        return f"scrypt-v1${salt.hex()}${digest.hex()}"

    def verify(self, secret: SecretInput, encoded_hash: str) -> bool:
        if len(encoded_hash) > 128:
            return False

        try:
            version, salt_hex, digest_hex = encoded_hash.split("$")
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except ValueError:
            return False

        if version != "scrypt-v1" or len(salt) != 16 or len(expected) != 32:
            return False

        actual = self._derive(secret, salt)
        return compare_digest(actual, expected)
