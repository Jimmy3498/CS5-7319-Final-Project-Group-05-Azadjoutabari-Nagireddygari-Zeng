import json
from hashlib import sha256
from pathlib import Path

from ..errors import ApplicationError, ErrorCode
from ..models.common import SecretInput


class LocalCredentialStore:
    def __init__(self, private_root: Path) -> None:
        self.private_root = private_root

    def _entry(self, owner_id: str, reference: str) -> tuple[str, str]:
        if not owner_id.strip() or not reference.strip():
            raise ApplicationError(ErrorCode.INVALID, "Missing credential identity")

        installation = sha256(str(self.private_root.resolve()).encode("utf-8")).hexdigest()
        entry = sha256(
            json.dumps([owner_id, reference], ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        return f"cs7319-storytelling:{installation}", entry

    def _backend(self):
        try:
            import keyring

            backend = keyring.get_keyring()
        except Exception:
            raise ApplicationError(
                ErrorCode.STORAGE, "Install keyring and enable an OS credential vault"
            ) from None

        allowed = {
            ("keyring.backends.macOS", "Keyring"),
            ("keyring.backends.Windows", "WinVaultKeyring"),
            ("keyring.backends.SecretService", "Keyring"),
            ("keyring.backends.libsecret", "Keyring"),
        }
        identity = (type(backend).__module__, type(backend).__name__)
        if identity not in allowed:
            raise ApplicationError(
                ErrorCode.STORAGE, "No supported OS credential vault is selected"
            )

        return backend

    def read(self, owner_id: str, reference: str) -> SecretInput:
        service, entry = self._entry(owner_id, reference)
        backend = self._backend()

        try:
            value = backend.get_password(service, entry)
        except Exception:
            raise ApplicationError(
                ErrorCode.STORAGE, "Could not read the credential vault"
            ) from None

        if not isinstance(value, str) or not value:
            raise ApplicationError(ErrorCode.INVALID, "API key not found")

        return SecretInput(value)

    def write(self, owner_id: str, reference: str, value: SecretInput) -> None:
        if not value.value.strip():
            raise ApplicationError(ErrorCode.INVALID, "API key is empty")

        service, entry = self._entry(owner_id, reference)
        backend = self._backend()

        try:
            backend.set_password(service, entry, value.value)
        except Exception:
            raise ApplicationError(
                ErrorCode.STORAGE, "Could not save the credential in the vault"
            ) from None

    def remove(self, owner_id: str, reference: str) -> None:
        service, entry = self._entry(owner_id, reference)
        backend = self._backend()

        try:
            if backend.get_password(service, entry) is not None:
                backend.delete_password(service, entry)
        except Exception:
            raise ApplicationError(
                ErrorCode.STORAGE, "Could not remove the credential from the vault"
            ) from None
