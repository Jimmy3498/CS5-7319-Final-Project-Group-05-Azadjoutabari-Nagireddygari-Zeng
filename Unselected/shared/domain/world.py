import json
from dataclasses import asdict, fields, is_dataclass
from math import isfinite

from ..codec import decode, load_json
from ..commands import CreateCampaign, PublishCampaign, SaveDraft
from ..errors import ApplicationError, ErrorCode
from ..models.common import Access, Condition, ConditionKind, Issue, Visibility
from ..models.world import CampaignContent, CampaignDraft, CatalogEntry, NumericStat, TextRecord
from ..ports import CampaignRepository


class WorldService:
    def __init__(self, repository: CampaignRepository) -> None:
        self.repository = repository

    def create(self, owner_id: str, command: CreateCampaign) -> CampaignDraft:
        title = command.title.strip()

        if not title:
            raise ApplicationError(ErrorCode.INVALID, "Campaign title is required")

        return self.repository.create_campaign(owner_id, title, command.summary.strip())

    def save(self, owner_id: str, command: SaveDraft) -> CampaignDraft:
        try:
            encoded = json.dumps(
                asdict(command.content),
                ensure_ascii=False,
                allow_nan=False,
            )
            content = decode(load_json(encoded), CampaignContent)
        except (TypeError, ValueError) as error:
            raise ApplicationError(
                ErrorCode.INVALID,
                "Invalid campaign data or draft exceeds 1 MB",
            ) from error

        return self.repository.save_draft(
            owner_id,
            command.campaign_id,
            command.expected_revision,
            content,
        )

    def readiness(self, content: CampaignContent) -> tuple[Issue, ...]:
        issues = []

        def add(path, code, message):
            issues.append(Issue(path, code, message))

        required = {
            "title": content.title,
            "summary": content.summary,
            "background": content.background.text,
            "opening": content.opening.text,
        }
        for path, value in required.items():
            if not value.strip():
                add(path, "required", f"{path.capitalize()} is required")

        if content.opening.access.visibility != Visibility.PUBLIC:
            add("opening", "visibility", "The opening must be public")

        world = content.world
        location_ids = {location.id for location in world.locations}

        if not world.locations:
            add("locations", "required", "Add at least one location")
        if not any(character.playable for character in world.characters):
            add("characters", "required", "Add at least one playable character")

        seen = set()
        for group, entities in (
            ("characters", world.characters),
            ("locations", world.locations),
        ):
            for entity in entities:
                if not entity.id.strip() or entity.id in seen:
                    add(
                        group,
                        "invalid_id",
                        "Character/location IDs must be nonempty and unique",
                    )
                seen.add(entity.id)
                if not entity.name.strip():
                    add(entity.id, "required", "A name is required")

        for character in world.characters:
            if character.location_id not in location_ids:
                add(
                    character.id,
                    "location",
                    "Starting location does not exist",
                )

        for location in world.locations:
            links = location.connected_location_ids
            if len(links) != len(set(links)) or any(
                target not in location_ids or target == location.id for target in links
            ):
                add(
                    location.id,
                    "connections",
                    "Location links must be unique, valid and not self-links",
                )

        character_ids = {character.id for character in world.characters}
        template_ids = {template.id for template in world.item_templates}
        condition_targets = {
            ConditionKind.AT_LOCATION: location_ids,
            ConditionKind.OWNS_ITEM: {item.id for item in world.items},
            ConditionKind.FLAG_EQUALS: {flag.id for flag in world.flags},
        }

        def walk(value):
            if is_dataclass(value):
                yield value
                for field in fields(value):
                    yield from walk(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    yield from walk(item)

        records = tuple(walk(content))
        record_ids = set()
        for record in records:
            record_id = getattr(record, "id", None)
            if record_id is not None:
                if not record_id.strip() or record_id in record_ids:
                    add(
                        "world",
                        "invalid_id",
                        "All record IDs must be nonempty and unique",
                    )
                record_ids.add(record_id)

            if isinstance(record, Access):
                if any(reader not in character_ids for reader in record.character_ids):
                    add(
                        "access",
                        "unknown_reader",
                        "Access refers to an unknown character",
                    )

            if isinstance(record, Condition):
                targets = condition_targets.get(record.kind, set())
                if record.target_id not in targets:
                    add("condition", "unknown_target", "Condition target does not exist")

            if isinstance(record, TextRecord):
                if record.stance not in {"authored_fact", "belief", "claim", "hearsay"}:
                    add(record.id, "stance", "Unknown statement type")
                if (
                    record.holder_id is not None
                    and record.holder_id not in character_ids
                    or record.stance != "authored_fact"
                    and record.holder_id not in character_ids
                ):
                    add(
                        record.id,
                        "holder",
                        "Statement holder must identify an existing character",
                    )

            if isinstance(record, NumericStat):
                values = (record.minimum, record.value, record.maximum)
                if not all(isfinite(value) for value in values) or not (
                    record.minimum <= record.value <= record.maximum
                ):
                    add(record.id, "stat_range", "Stat value must be within finite bounds")

        for item in world.items:
            if item.template_id not in template_ids:
                add(item.id, "template", "Item template does not exist")
            if (item.holder_id is None) == (item.location_id is None):
                add(item.id, "placement", "An item needs exactly one holder or location")
            if item.holder_id is not None and item.holder_id not in character_ids:
                add(item.id, "holder", "Item holder does not exist")
            if item.location_id is not None and item.location_id not in location_ids:
                add(item.id, "location", "Item location does not exist")

        for template in world.item_templates:
            count = sum(item.template_id == template.id for item in world.items)
            if template.max_instances_per_session < 1 or count > template.max_instances_per_session:
                add(
                    template.id,
                    "item_limit",
                    "Template limit must cover its initial items",
                )

        for relationship in world.relationships:
            if (
                relationship.source_id not in character_ids
                or relationship.target_id not in character_ids
                or relationship.source_id == relationship.target_id
            ):
                add(
                    relationship.id,
                    "relationship",
                    "Relationship needs two distinct characters",
                )

        stats = {record.id: record for record in records if isinstance(record, NumericStat)}
        for band in world.stat_bands:
            stat = stats.get(band.stat_id)
            if (
                stat is None
                or not all(isfinite(value) for value in (band.minimum, band.maximum))
                or not stat.minimum <= band.minimum < band.maximum <= stat.maximum
            ):
                add(
                    "stat_bands",
                    "range",
                    "Stat band must reference a stat and fit its bounds",
                )

        for stat_id in stats:
            bands = sorted(
                (band for band in world.stat_bands if band.stat_id == stat_id),
                key=lambda band: band.minimum,
            )
            if any(left.maximum > right.minimum for left, right in zip(bands, bands[1:])):
                add(
                    "stat_bands",
                    "overlap",
                    "Bands for the same stat must not overlap",
                )

        return tuple(issues)

    def publish(self, owner_id: str, command: PublishCampaign) -> CatalogEntry:
        draft = self.repository.load_draft(owner_id, command.campaign_id)

        if draft.revision != command.expected_revision:
            raise ApplicationError(ErrorCode.CONFLICT, "Draft changed; reload before publishing")

        issues = self.readiness(draft.content)
        if issues:
            raise ApplicationError(ErrorCode.INVALID, "; ".join(issue.message for issue in issues))

        published = self.repository.publish(
            owner_id, command.campaign_id, command.expected_revision
        )
        return CatalogEntry(
            campaign_id=published.campaign_id,
            published_version_id=published.id,
            title=published.content.title,
            summary=published.content.summary,
            version=published.version,
            playable_character_names=tuple(
                character.name
                for character in published.content.world.characters
                if character.playable
            ),
            playable_character_ids=tuple(
                character.id
                for character in published.content.world.characters
                if character.playable
            ),
        )
