from dataclasses import fields, is_dataclass, replace

from ..commands import AdoptCampaignUpdate
from ..errors import ApplicationError, ErrorCode
from ..models.gameplay import MergeConflict, MergePreview, SessionSnapshot
from ..ports import CampaignRepository, SessionRepository
from .world import WorldService


class UpdateService:
    """Conservative three-way campaign-version adoption using stable entity IDs."""

    def __init__(self, campaigns: CampaignRepository, sessions: SessionRepository) -> None:
        self.campaigns = campaigns
        self.sessions = sessions

    @staticmethod
    def _identity(value: object) -> str:
        identity = getattr(value, "id", None)
        if isinstance(identity, str):
            return identity
        return repr((getattr(value, "stat_id", None), getattr(value, "minimum", None)))

    def _merge_group(
        self, group: str, old: tuple, new: tuple, current: tuple
    ) -> tuple[tuple, tuple[MergeConflict, ...]]:
        old_by = {self._identity(value): value for value in old}
        new_by = {self._identity(value): value for value in new}
        current_by = {self._identity(value): value for value in current}
        conflicts: list[MergeConflict] = []
        merged: dict[str, object] = {}
        for identity in set(old_by) | set(new_by) | set(current_by):
            before, authored, played = (
                old_by.get(identity),
                new_by.get(identity),
                current_by.get(identity),
            )
            if authored == before:
                chosen = played
            elif played == before or played == authored:
                chosen = authored
            elif before is None and authored is None:
                chosen = played
            elif (
                before is not None
                and authored is not None
                and played is not None
                and type(before) is type(authored) is type(played)
            ):
                chosen, nested = self._merge_value(group, identity, before, authored, played)
                conflicts.extend(nested)
            else:
                conflicts.append(MergeConflict(identity, group, "author_and_session_changed"))
                chosen = played
            if chosen is not None:
                merged[identity] = chosen
        order = [self._identity(value) for value in new]
        order.extend(
            self._identity(value) for value in current if self._identity(value) not in order
        )
        return tuple(merged[key] for key in order if key in merged), tuple(conflicts)

    def _merge_value(
        self, path: str, identity: str, before: object, authored: object, played: object
    ) -> tuple[object, tuple[MergeConflict, ...]]:
        if authored == before:
            return played, ()
        if played == before or played == authored:
            return authored, ()
        if is_dataclass(before) and type(before) is type(authored) is type(played):
            changes = {}
            conflicts: list[MergeConflict] = []
            for field in fields(before):
                old_value = getattr(before, field.name)
                new_value = getattr(authored, field.name)
                current_value = getattr(played, field.name)
                if isinstance(old_value, tuple) and all(
                    self._identity(value) != repr((None, None)) for value in old_value
                ):
                    value, found = self._merge_group(
                        f"{path}.{field.name}", old_value, new_value, current_value
                    )
                else:
                    value, found = self._merge_value(
                        f"{path}.{field.name}",
                        identity,
                        old_value,
                        new_value,
                        current_value,
                    )
                changes[field.name] = value
                conflicts.extend(found)
            return replace(played, **changes), tuple(conflicts)
        return played, (MergeConflict(identity, path, "author_and_session_changed"),)

    def preview(self, owner_id: str, session_id: str, target_version_id: str) -> MergePreview:
        current = self.sessions.load_session(owner_id, session_id)
        old = self.campaigns.published(current.base_version_id)
        target = self.campaigns.published(target_version_id)
        if target.campaign_id != current.campaign_id or old.campaign_id != current.campaign_id:
            raise ApplicationError(ErrorCode.INVALID, "Campaign update belongs to another campaign")
        if target.id == current.base_version_id:
            raise ApplicationError(ErrorCode.INVALID, "Session already uses this campaign version")

        changes: dict[str, tuple] = {}
        conflicts: list[MergeConflict] = []
        for field in fields(current.world):
            merged, found = self._merge_group(
                field.name,
                getattr(old.content.world, field.name),
                getattr(target.content.world, field.name),
                getattr(current.world, field.name),
            )
            changes[field.name] = merged
            conflicts.extend(found)
        world = replace(current.world, **changes)

        setup: list = []
        for name, index in (("background", 0), ("opening", 1)):
            before = getattr(old.content, name)
            authored = getattr(target.content, name)
            played = current.setup[index] if len(current.setup) > index else before
            if authored == before:
                setup.append(played)
            elif played == before or played == authored:
                setup.append(authored)
            else:
                conflicts.append(MergeConflict(played.id, name, "author_and_session_changed"))
                setup.append(played)

        if current.player_character_id not in {character.id for character in world.characters}:
            conflicts.append(
                MergeConflict(current.player_character_id, "characters", "player_character_removed")
            )
        current_flags = dict(current.flag_values)
        flags = tuple((flag.id, current_flags.get(flag.id, flag.initial)) for flag in world.flags)
        candidate = replace(
            current,
            base_version_id=target.id,
            world=world,
            setup=tuple(setup),
            flag_values=flags,
        )
        readiness = WorldService(self.campaigns).readiness(
            replace(target.content, background=setup[0], opening=setup[1], world=world)
        )
        if conflicts or readiness:
            candidate = None
        return MergePreview(
            current.id,
            current.revision,
            old.id,
            target.id,
            tuple(conflicts),
            readiness,
            candidate,
        )

    def adopt(
        self, owner_id: str, operation_id: str, command: AdoptCampaignUpdate
    ) -> SessionSnapshot:
        preview = self.preview(owner_id, command.session_id, command.target_version_id)
        if preview.expected_session_revision != command.expected_session_revision:
            raise ApplicationError(ErrorCode.CONFLICT, "Session changed; preview the update again")
        if preview.candidate is None:
            raise ApplicationError(ErrorCode.CONFLICT, "Campaign update has merge conflicts")
        return self.sessions.adopt_update(
            owner_id,
            operation_id,
            command.expected_session_revision,
            preview.candidate,
        )
