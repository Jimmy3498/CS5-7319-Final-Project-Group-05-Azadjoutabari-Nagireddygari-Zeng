from dataclasses import fields, is_dataclass, replace

from ..errors import ApplicationError, ErrorCode
from ..models.ai import DirectorContext, PlayerView, VisibleFact
from ..models.common import Visibility
from ..models.gameplay import AcceptedTurn, EffectKind, SessionSnapshot, TurnProposal
from ..models.world import ItemInstance, TextRecord
from .conditions import condition_met


class KnowledgePolicy:
    def director_context(
        self,
        session: SessionSnapshot,
        player_input: str,
        history: tuple[AcceptedTurn, ...] = (),
    ) -> DirectorContext:
        pc = next(
            (
                character
                for character in session.world.characters
                if character.id == session.player_character_id
            ),
            None,
        )
        if pc is None or any(turn.session_id != session.id for turn in history):
            raise ApplicationError(ErrorCode.INVALID, "Invalid director context")

        return DirectorContext(
            campaign_id=session.campaign_id,
            session_id=session.id,
            revision=session.revision,
            world=session.world,
            player_input=player_input,
            player_character_id=pc.id,
            flag_values=session.flag_values,
            disclosed_ids=session.disclosed_ids,
            setup=session.setup,
            accepted_history=tuple(
                f"Player: {turn.player_input}\nNarrator: {turn.narration}"
                for turn in sorted(history, key=lambda turn: turn.revision)
            ),
        )

    def authorize_disclosure(
        self, session: SessionSnapshot, target_id: str, actor_id: str
    ) -> SessionSnapshot:
        if actor_id != session.player_character_id:
            raise ApplicationError(ErrorCode.INVALID, "Disclosure recipient must be the player")
        if target_id in session.disclosed_ids:
            return session

        def records(value):
            if isinstance(value, (TextRecord, ItemInstance)):
                yield value
            if is_dataclass(value):
                for field in fields(value):
                    yield from records(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    yield from records(item)

        target = next(
            (record for record in records(session) if record.id == target_id),
            None,
        )
        if isinstance(target, TextRecord):
            allowed = condition_met(target.reveal_when, session)
        elif isinstance(target, ItemInstance):
            pc = next(c for c in session.world.characters if c.id == session.player_character_id)
            allowed = (
                target.holder_id == pc.id or target.location_id == pc.location_id
            ) and condition_met(target.discover_when, session)
        else:
            allowed = False

        if not allowed:
            raise ApplicationError(ErrorCode.INVALID, "Disclosure is not authorized")

        return replace(session, disclosed_ids=(*session.disclosed_ids, target_id))

    def authorize_disclosures(
        self, before: SessionSnapshot, candidate: SessionSnapshot, proposal: TurnProposal
    ) -> SessionSnapshot:
        # Individual gates were checked during ordered effect validation.
        identity = (
            "id",
            "owner_id",
            "campaign_id",
            "base_version_id",
            "revision",
            "player_character_id",
        )
        if proposal.expected_revision != before.revision or any(
            getattr(before, name) != getattr(candidate, name) for name in identity
        ):
            raise ApplicationError(ErrorCode.INVALID, "Mismatched turn context")

        requested = tuple(
            effect.target_id
            for effect in proposal.effects
            if effect.kind == EffectKind.REQUEST_DISCLOSURE
        )
        expected = tuple(dict.fromkeys((*before.disclosed_ids, *requested)))
        if candidate.disclosed_ids != expected:
            raise ApplicationError(ErrorCode.INVALID, "Unexpected disclosure change")

        visible_ids = {fact.id for fact in self.player_view(candidate, (), "").facts}
        if not set(proposal.public_fact_ids) <= visible_ids:
            raise ApplicationError(
                ErrorCode.INVALID, "Plan cites information outside the player view"
            )

        return candidate

    def player_view(
        self, session: SessionSnapshot, history: tuple[AcceptedTurn, ...], player_input: str
    ) -> PlayerView:
        world = session.world
        pc = next(
            (c for c in world.characters if c.id == session.player_character_id),
            None,
        )
        if pc is None or any(turn.session_id != session.id for turn in history):
            raise ApplicationError(ErrorCode.INVALID, "Invalid player view context")

        facts = {}
        disclosed = set(session.disclosed_ids)

        def readable(access):
            return access.visibility == Visibility.PUBLIC or (
                access.visibility == Visibility.CHARACTER and pc.id in access.character_ids
            )

        def text(record, fact_id=None, prefix=""):
            if readable(record.access) or record.id in disclosed:
                attribution = (
                    f"[{record.stance} held by {record.holder_id}] "
                    if record.stance != "authored_fact"
                    else ""
                )
                ident = fact_id or record.id
                facts[ident] = VisibleFact(ident, prefix + attribution + record.text, record.stance)

        def stats(entries):
            for stat in entries:
                if not readable(stat.access):
                    continue
                value = f"{stat.name}: {stat.value:g}"
                for band in world.stat_bands:
                    if (
                        band.stat_id == stat.id
                        and band.minimum <= stat.value
                        and (
                            stat.value < band.maximum or stat.value == band.maximum == stat.maximum
                        )
                    ):
                        value += f" — {band.text}"
                facts[stat.id] = VisibleFact(stat.id, value, "authored_fact")

        for record in (*session.setup, *world.canon, *world.narrative_rules):
            text(record)

        for location in world.locations:
            if location.id == pc.location_id:
                text(location.description, prefix=f"{location.name}: ")

        for character in world.characters:
            if character.id != pc.id and character.location_id != pc.location_id:
                continue
            for record in (
                character.appearance,
                character.expression_style,
                character.personality,
                character.personal_history,
                *character.knowledge,
            ):
                text(record, prefix=f"{character.name}: ")
            stats(character.stats)

        templates = {template.id: template for template in world.item_templates}
        for item in world.items:
            nearby = item.holder_id == pc.id or item.location_id == pc.location_id
            if not nearby or not (readable(item.access) or item.id in disclosed):
                continue

            template = templates[item.template_id]
            placement = "carried by you" if item.holder_id == pc.id else "nearby"
            text(
                template.description,
                fact_id=item.id,
                prefix=f"{template.name} ({placement}): ",
            )

        for relationship in world.relationships:
            if pc.id in (relationship.source_id, relationship.target_id):
                text(relationship.description)
                stats(relationship.stats)

        values = dict(session.flag_values)
        for flag in world.flags:
            if readable(flag.access):
                facts[flag.id] = VisibleFact(
                    flag.id,
                    f"{flag.label}: {values[flag.id]}",
                    "authored_fact",
                )

        return PlayerView(
            session_id=session.id,
            revision=session.revision,
            player_character_id=pc.id,
            facts=tuple(facts.values()),
            accepted_history=tuple(
                f"Player: {turn.player_input}\nNarrator: {turn.narration}"
                for turn in sorted(history, key=lambda turn: turn.revision)
            ),
            player_input=player_input,
        )
