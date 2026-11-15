import json
from dataclasses import asdict, replace
from math import isfinite
from uuid import uuid4

from ..codec import decode, load_json
from ..commands import RetryTurn, StartSession, SubmitTurn
from ..errors import ApplicationError, ErrorCode, unfinished
from ..models.common import Visibility
from ..models.gameplay import (
    AcceptedTurn,
    EffectKind,
    SessionSnapshot,
    TurnAdmission,
    TurnCommit,
    TurnProposal,
)
from ..models.world import ItemInstance
from ..ports import CampaignRepository, SessionRepository
from .conditions import condition_met
from .knowledge import KnowledgePolicy


class GameplayService:
    def __init__(self, campaigns: CampaignRepository, sessions: SessionRepository) -> None:
        self.campaigns = campaigns
        self.sessions = sessions

    def start(self, owner_id: str, command: StartSession) -> SessionSnapshot:
        campaign = self.campaigns.published(command.published_version_id)
        character = next(
            (
                character
                for character in campaign.content.world.characters
                if character.id == command.player_character_id
            ),
            None,
        )
        if character is None or not character.playable:
            raise ApplicationError(
                ErrorCode.INVALID, "Choose a playable character from this campaign"
            )

        return self.sessions.start_session(owner_id, campaign, character.id)

    def prepare(
        self, owner_id: str, command: SubmitTurn, operation_id: str, request_id: str
    ) -> TurnAdmission:
        if not command.player_input.strip() or len(command.player_input) > 8000:
            raise ApplicationError(
                ErrorCode.INVALID, "Enter an action between 1 and 8,000 characters"
            )
        if type(command.expected_revision) is not int or command.expected_revision < 0:
            raise ApplicationError(ErrorCode.INVALID, "Invalid session revision")
        if not operation_id.strip() or not request_id.strip():
            raise ApplicationError(ErrorCode.INVALID, "Missing request identity")

        return self.sessions.admit_turn(
            owner_id=owner_id,
            operation_id=operation_id,
            request_id=request_id,
            session_id=command.session_id,
            expected_revision=command.expected_revision,
            player_input=command.player_input,
        )

    def validate_and_project(
        self, baseline: SessionSnapshot, proposal: TurnProposal
    ) -> SessionSnapshot:
        try:
            encoded = json.dumps(asdict(proposal), allow_nan=False)
            proposal = decode(load_json(encoded), TurnProposal)
        except (TypeError, ValueError) as error:
            raise ApplicationError(ErrorCode.INVALID, "Invalid turn proposal") from error

        if proposal.expected_revision != baseline.revision:
            raise ApplicationError(ErrorCode.CONFLICT, "Plan uses a stale session revision")

        def invalid(message):
            raise ApplicationError(ErrorCode.INVALID, message)

        if len(proposal.effects) > 32 or len(proposal.public_fact_ids) > 64:
            invalid("Plan exceeds effect or fact limits")

        required = {
            EffectKind.MOVE_CHARACTER: {"destination_id"},
            EffectKind.TRANSFER_ITEM: {"target_id", "destination_id"},
            EffectKind.CHANGE_STAT: {"target_id", "stat_id", "delta"},
            EffectKind.CHANGE_RELATIONSHIP_STAT: {"target_id", "stat_id", "delta"},
            EffectKind.SET_FLAG: {"target_id", "enabled"},
            EffectKind.REQUEST_DISCLOSURE: {"target_id"},
            EffectKind.CREATE_ITEM: {"template_id", "destination_id"},
        }
        fields = {
            "target_id",
            "destination_id",
            "stat_id",
            "delta",
            "enabled",
            "template_id",
        }
        world = baseline.world
        flags = dict(baseline.flag_values)
        disclosed = baseline.disclosed_ids

        for effect in proposal.effects:
            supplied = {name for name in fields if getattr(effect, name) is not None}
            if supplied != required[effect.kind]:
                invalid("Incorrect fields for this effect")

            characters = {character.id: character for character in world.characters}
            actor = characters.get(effect.actor_id)
            if actor is None:
                invalid("Unknown actor")

            if effect.kind == EffectKind.MOVE_CHARACTER:
                locations = {location.id: location for location in world.locations}
                origin = locations.get(actor.location_id)
                if (
                    origin is None
                    or effect.destination_id not in locations
                    or effect.destination_id not in origin.connected_location_ids
                ):
                    invalid("Destination must be an adjacent location")

                world = replace(
                    world,
                    characters=tuple(
                        replace(character, location_id=effect.destination_id)
                        if character.id == actor.id
                        else character
                        for character in world.characters
                    ),
                )

            elif effect.kind in (
                EffectKind.CHANGE_STAT,
                EffectKind.CHANGE_RELATIONSHIP_STAT,
            ):
                collection = (
                    "characters" if effect.kind == EffectKind.CHANGE_STAT else "relationships"
                )
                entries = getattr(world, collection)
                target = next(
                    (entry for entry in entries if entry.id == effect.target_id),
                    None,
                )
                if target is None:
                    invalid("Unknown stat owner")
                if collection == "relationships" and actor.id not in (
                    target.source_id,
                    target.target_id,
                ):
                    invalid("Actor must participate in the relationship")

                stat = next(
                    (stat for stat in target.stats if stat.id == effect.stat_id),
                    None,
                )
                if stat is None:
                    invalid("Unknown stat")

                value = stat.value + effect.delta
                if not isfinite(value) or not stat.minimum <= value <= stat.maximum:
                    invalid("Stat change exceeds its bounds")

                updated = replace(
                    target,
                    stats=tuple(
                        replace(item, value=value) if item.id == stat.id else item
                        for item in target.stats
                    ),
                )
                world = replace(
                    world,
                    **{
                        collection: tuple(
                            updated if entry.id == target.id else entry for entry in entries
                        )
                    },
                )
            elif effect.kind in (EffectKind.TRANSFER_ITEM, EffectKind.CREATE_ITEM):
                recipient = characters.get(effect.destination_id)
                if effect.destination_id == actor.location_id:
                    holder_id, location_id = None, actor.location_id
                elif recipient is not None and recipient.location_id == actor.location_id:
                    holder_id, location_id = recipient.id, None
                else:
                    invalid("Item destination must be at the actor's location")

                if effect.kind == EffectKind.TRANSFER_ITEM:
                    item = next(
                        (item for item in world.items if item.id == effect.target_id),
                        None,
                    )
                    if item is None or not (
                        item.holder_id == actor.id or item.location_id == actor.location_id
                    ):
                        invalid("Item is not available to this actor")

                    readable = (
                        item.access.visibility == Visibility.PUBLIC
                        or (
                            item.access.visibility == Visibility.CHARACTER
                            and actor.id in item.access.character_ids
                        )
                        or (actor.id == baseline.player_character_id and item.id in disclosed)
                    )
                    if not readable:
                        invalid("Item has not been revealed to this actor")

                    world = replace(
                        world,
                        items=tuple(
                            replace(
                                item,
                                holder_id=holder_id,
                                location_id=location_id,
                            )
                            if item.id == effect.target_id
                            else item
                            for item in world.items
                        ),
                    )

                else:
                    template = next(
                        (
                            template
                            for template in world.item_templates
                            if template.id == effect.template_id
                        ),
                        None,
                    )
                    current = replace(baseline, world=world, flag_values=tuple(flags.items()))
                    if template is None or not condition_met(template.spawn_when, current):
                        invalid("Item template is not available here")

                    count = sum(item.template_id == template.id for item in world.items)
                    if count >= template.max_instances_per_session:
                        invalid("Item template limit reached")

                    created = ItemInstance(
                        id=str(uuid4()),
                        template_id=template.id,
                        holder_id=holder_id,
                        location_id=location_id,
                        access=template.description.access,
                    )
                    world = replace(world, items=world.items + (created,))
            elif effect.kind == EffectKind.SET_FLAG:
                flag = next(
                    (flag for flag in world.flags if flag.id == effect.target_id),
                    None,
                )
                current = replace(baseline, world=world, flag_values=tuple(flags.items()))
                if (
                    flag is None
                    or flag.id not in flags
                    or not condition_met(flag.set_when, current)
                ):
                    invalid("Flag condition is not satisfied")

                flags[flag.id] = effect.enabled
            elif effect.kind == EffectKind.REQUEST_DISCLOSURE:
                current = replace(
                    baseline,
                    world=world,
                    flag_values=tuple(flags.items()),
                    disclosed_ids=disclosed,
                )
                authorized = KnowledgePolicy().authorize_disclosure(
                    current, effect.target_id, actor.id
                )
                disclosed = authorized.disclosed_ids

            else:
                invalid("Unknown effect kind")

        return replace(
            baseline, world=world, flag_values=tuple(flags.items()), disclosed_ids=disclosed
        )

    def commit(
        self,
        owner_id: str,
        operation_id: str,
        command: SubmitTurn,
        candidate: SessionSnapshot,
        narration: str,
    ) -> TurnCommit:
        if candidate.id != command.session_id:
            raise ApplicationError(ErrorCode.INVALID, "Candidate belongs to a different session")

        return self.sessions.commit_turn(
            owner_id=owner_id,
            operation_id=operation_id,
            expected_revision=command.expected_revision,
            candidate=candidate,
            player_input=command.player_input,
            narration=narration,
        )

    def history(self, owner_id: str, session_id: str) -> tuple[AcceptedTurn, ...]:
        return self.sessions.accepted_history(owner_id, session_id)

    def retry(
        self, owner_id: str, command: RetryTurn, new_operation_id: str
    ) -> SubmitTurn | TurnCommit:
        """TODO GAMEPLAY: inspect prior durable receipt/attempt, preserve input, explicitly link
        retry; committed old operation returns its result, never repeats billing. Unknown remote
        outcome stays visible. Fence late old replies; no automatic paid replay after restart.
        """
        unfinished("GAMEPLAY")

    def record_failure(
        self,
        owner_id: str,
        operation_id: str,
        session_id: str,
        public_code: str,
        provider_uncertain: bool,
    ) -> None:
        self.sessions.record_attempt_failure(
            owner_id,
            operation_id,
            session_id,
            public_code,
            provider_uncertain,
        )
