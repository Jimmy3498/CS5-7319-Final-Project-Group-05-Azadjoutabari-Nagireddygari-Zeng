import logging
from ipaddress import ip_address
from urllib.parse import urlsplit
from uuid import uuid4

from ..commands import ActivateProviderCard, DeleteProviderCard, SaveProviderCard
from ..errors import ApplicationError, ErrorCode
from ..models.ai import ProviderCapture, ProviderProfile
from ..ports import CredentialStore, ProfileRepository


class ProfileService:
    def __init__(self, repository: ProfileRepository, credentials: CredentialStore) -> None:
        self.repository = repository
        self.credentials = credentials

    def _retire_credential(self, owner_id: str, reference: str) -> None:
        try:
            self.credentials.remove(owner_id, reference)
        except Exception:
            logging.getLogger(__name__).warning(
                "Unused API credential could not be removed from the vault"
            )

    def save(self, owner_id: str, command: SaveProviderCard) -> ProviderProfile:
        name, endpoint, model = (
            command.name.strip(),
            command.endpoint.strip(),
            command.model.strip(),
        )
        if not name or not model:
            raise ApplicationError(ErrorCode.INVALID, "Card name and model are required")
        if command.protocol != "chat-completions-compatible":
            raise ApplicationError(ErrorCode.INVALID, "Unsupported provider protocol")

        try:
            url = urlsplit(endpoint)
            host = url.hostname
            port = url.port
            try:
                loopback = ip_address(host or "").is_loopback
            except ValueError:
                loopback = host == "localhost"

            valid = (
                bool(host)
                and url.username is None
                and url.password is None
                and not url.fragment
                and port != 0
                and not any(character.isspace() for character in endpoint)
                and (url.scheme == "https" or (url.scheme == "http" and loopback))
            )
        except ValueError:
            valid = False

        if not valid:
            raise ApplicationError(ErrorCode.INVALID, "Use an HTTPS endpoint or loopback HTTP URL")

        if command.credential is not None and not command.credential.value.strip():
            raise ApplicationError(ErrorCode.INVALID, "API key is empty")

        old_reference = None
        if command.profile_id is None:
            if command.expected_revision is not None:
                raise ApplicationError(ErrorCode.INVALID, "New cards have no revision")
            profile_id = str(uuid4())
        else:
            if type(command.expected_revision) is not int or command.expected_revision < 1:
                raise ApplicationError(ErrorCode.INVALID, "Invalid card revision")

            existing = next(
                (
                    card
                    for card in self.repository.list_profiles(owner_id)
                    if card.id == command.profile_id
                ),
                None,
            )
            if existing is None:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card not found")
            if existing.configuration_revision != command.expected_revision:
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Provider card changed; reload before saving"
                )

            profile_id = existing.id
            if existing.credential_present:
                old_reference = self.repository.credential_reference(
                    owner_id, existing.id, existing.configuration_revision
                )

            if (
                old_reference is not None
                and endpoint != existing.endpoint
                and command.credential is None
            ):
                raise ApplicationError(
                    ErrorCode.INVALID, "Re-enter the API key when changing the endpoint"
                )

        reference = old_reference
        if command.credential is not None:
            reference = str(uuid4())
            self.credentials.write(owner_id, reference, command.credential)

        profile = ProviderProfile(
            profile_id,
            owner_id,
            name,
            endpoint,
            model,
            command.protocol,
            0,
            reference is not None,
        )

        try:
            saved = self.repository.save_profile(
                owner_id, profile, command.expected_revision, reference
            )
        except Exception:
            if reference is not None and reference != old_reference:
                self._retire_credential(owner_id, reference)
            raise

        if old_reference is not None and old_reference != reference:
            self._retire_credential(owner_id, old_reference)

        return saved

    def activate(self, owner_id: str, command: ActivateProviderCard) -> ProviderProfile:
        reference = self.repository.credential_reference(
            owner_id, command.profile_id, command.expected_revision
        )
        self.credentials.read(owner_id, reference)

        return self.repository.activate_profile(
            owner_id, command.profile_id, command.expected_revision
        )

    def delete(self, owner_id: str, command: DeleteProviderCard) -> None:
        cards = {card.id: card for card in self.repository.list_profiles(owner_id)}
        card = cards.get(command.profile_id)
        if card is None:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card not found")
        if card.configuration_revision != command.expected_revision:
            raise ApplicationError(
                ErrorCode.CONFLICT, "Provider card changed; reload before deleting"
            )

        reference = None
        if card.credential_present:
            reference = self.repository.credential_reference(
                owner_id, card.id, command.expected_revision
            )

        if command.replacement_profile_id is not None:
            replacement = cards.get(command.replacement_profile_id)
            if replacement is None:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Replacement card not found")

            replacement_reference = self.repository.credential_reference(
                owner_id,
                replacement.id,
                replacement.configuration_revision,
            )
            self.credentials.read(owner_id, replacement_reference)

        self.repository.delete_profile(
            owner_id,
            command.profile_id,
            command.expected_revision,
            command.replacement_profile_id,
        )

        if reference is not None:
            self._retire_credential(owner_id, reference)

    def capture(self, owner_id: str) -> ProviderCapture:
        profile = self.repository.active_profile(owner_id)
        reference = self.repository.credential_reference(
            owner_id, profile.id, profile.configuration_revision
        )
        credential = self.credentials.read(owner_id, reference)

        return ProviderCapture(profile, credential)
