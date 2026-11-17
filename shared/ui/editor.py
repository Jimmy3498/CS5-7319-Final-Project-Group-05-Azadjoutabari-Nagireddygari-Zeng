import json
from dataclasses import fields, is_dataclass, replace
from hashlib import sha256
from uuid import uuid4

import streamlit as st

from shared.codec import decode, json_value, load_json
from shared.commands import (
    CreateCampaign,
    PublishCampaign,
    RequestEditorProposal,
    ReviewEditorProposal,
    SaveDraft,
)
from shared.models.common import Access, Condition, ConditionKind, Visibility
from shared.models.world import (
    CampaignContent,
    CampaignDraft,
    Character,
    EditorBuffer,
    EditorProposal,
    FlagDefinition,
    ItemInstance,
    ItemTemplate,
    Location,
    MilestoneDefinition,
    NumericStat,
    Relationship,
    StatBand,
    TextRecord,
)
from shared.queries import Query, QueryKind

from .common import UIContext, consume_completed, query, require_account, submit

BUFFER_KEY = "ui.editor.buffer"
PROPOSAL_KEY = "ui.editor.proposal"
CAMPAIGN_KEY = "ui.editor.campaign_id"


def _record_ids(value: object) -> tuple[str, ...]:
    result: list[str] = []

    def visit(item: object) -> None:
        if is_dataclass(item):
            identity = getattr(item, "id", None)
            if isinstance(identity, str):
                result.append(identity)
            for field in fields(item):
                visit(getattr(item, field.name))
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return tuple(dict.fromkeys(result))


def _set_content(buffer: EditorBuffer, content: CampaignContent) -> None:
    st.session_state[BUFFER_KEY] = replace(
        buffer,
        local_revision=buffer.local_revision + 1,
        content=content,
    )
    st.session_state.pop(PROPOSAL_KEY, None)


def _load_buffer(ui: UIContext, campaign_id: str) -> EditorBuffer | None:
    draft = query(ui, Query(QueryKind.EDITOR_DRAFT, campaign_id))
    if not isinstance(draft, CampaignDraft):
        return None
    buffer = EditorBuffer(draft.id, draft.revision, 0, draft.content)
    st.session_state[BUFFER_KEY] = buffer
    st.session_state.pop(PROPOSAL_KEY, None)
    return buffer


def _setup_tab(buffer: EditorBuffer) -> None:
    content = buffer.content
    with st.form(f"editor_setup_{buffer.campaign_id}_{buffer.local_revision}"):
        title = st.text_input("Campaign title", value=content.title)
        summary = st.text_area("Public catalog summary", value=content.summary)
        background = st.text_area("Public setting/background", value=content.background.text)
        opening = st.text_area("Opening scene", value=content.opening.text)
        apply_setup = st.form_submit_button("Apply setup changes", type="primary")
    if apply_setup:
        _set_content(
            buffer,
            replace(
                content,
                title=title,
                summary=summary,
                background=replace(content.background, text=background),
                opening=replace(content.opening, text=opening),
            ),
        )
        st.rerun()


def _canon_tab(buffer: EditorBuffer) -> None:
    records = buffer.content.world.canon
    if records:
        st.dataframe(
            [
                {
                    "ID": record.id,
                    "Title": record.title,
                    "Statement": record.stance,
                    "Visibility": record.access.visibility.value,
                    "Text": record.text,
                }
                for record in records
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No canon or knowledge records yet.")
    with st.form(f"canon_add_{buffer.campaign_id}_{buffer.local_revision}"):
        title = st.text_input("Fact title")
        text = st.text_area("Fact text")
        visibility = st.selectbox("Visibility", tuple(Visibility), format_func=lambda v: v.value)
        add = st.form_submit_button("Add canon fact", type="primary")
    if add:
        identity = f"canon-{uuid4().hex[:12]}"
        record = TextRecord(identity, title.strip(), text.strip(), Access(visibility))
        world = replace(buffer.content.world, canon=(*records, record))
        _set_content(buffer, replace(buffer.content, world=world))
        st.rerun()
    if records:
        remove_id = st.selectbox(
            "Remove canon fact",
            [record.id for record in records],
            format_func=lambda identity: next(r.title for r in records if r.id == identity),
            key=f"canon_remove_{buffer.local_revision}",
        )
        if st.button("Remove selected fact", key=f"canon_remove_button_{buffer.local_revision}"):
            world = replace(
                buffer.content.world,
                canon=tuple(record for record in records if record.id != remove_id),
            )
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()


def _characters_tab(buffer: EditorBuffer) -> None:
    characters = buffer.content.world.characters
    if characters:
        st.dataframe(
            [
                {
                    "ID": character.id,
                    "Name": character.name,
                    "Playable": character.playable,
                    "Starting location": character.location_id,
                }
                for character in characters
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No characters yet.")
    locations = buffer.content.world.locations
    if not locations:
        st.info("Add a location before creating a character.")
        return
    choices = [None, *(character.id for character in characters)]
    selection_key = f"editor_character_{buffer.campaign_id}"
    if st.session_state.get(selection_key) not in choices:
        st.session_state[selection_key] = None
    selected_id = st.selectbox(
        "Character",
        choices,
        format_func=lambda identity: (
            "Add a character"
            if identity is None
            else next(c.name for c in characters if c.id == identity)
        ),
        key=selection_key,
    )
    selected = next((c for c in characters if c.id == selected_id), None)
    location_ids = [location.id for location in locations]
    if selected and selected.location_id not in location_ids:
        location_ids.append(selected.location_id)
    with st.form(f"character_form_{buffer.campaign_id}_{buffer.local_revision}_{selected_id}"):
        name = st.text_input("Character name", value=selected.name if selected else "")
        playable = st.checkbox("Playable preset", value=selected.playable if selected else False)
        location_id = st.selectbox(
            "Starting location",
            location_ids,
            index=location_ids.index(selected.location_id) if selected else 0,
            format_func=lambda identity: next(
                (location.name for location in locations if location.id == identity),
                f"Missing location: {identity}",
            ),
        )
        appearance = st.text_area("Appearance", value=selected.appearance.text if selected else "")
        personality = st.text_area(
            "Personality", value=selected.personality.text if selected else ""
        )
        history = st.text_area(
            "Personal history", value=selected.personal_history.text if selected else ""
        )
        expression = st.text_area(
            "Expression style", value=selected.expression_style.text if selected else ""
        )
        if selected is None:
            stat_name = st.text_input("Optional stat name")
            stat_value = st.number_input("Initial value", value=0.0)
        apply_character = st.form_submit_button(
            "Update character" if selected else "Add character", type="primary"
        )
    if apply_character:
        if selected:
            character = replace(
                selected,
                name=name.strip(),
                playable=playable,
                location_id=location_id,
                appearance=replace(selected.appearance, text=appearance.strip()),
                personality=replace(selected.personality, text=personality.strip()),
                personal_history=replace(selected.personal_history, text=history.strip()),
                expression_style=replace(selected.expression_style, text=expression.strip()),
            )
            updated = tuple(character if c.id == selected.id else c for c in characters)
        else:
            identity = f"character-{uuid4().hex[:12]}"
            public = Access(Visibility.PUBLIC)
            private = Access(Visibility.CHARACTER, (identity,))
            stats = ()
            if stat_name.strip():
                stats = (
                    NumericStat(
                        f"stat-{uuid4().hex[:12]}",
                        stat_name.strip(),
                        stat_value,
                        -100.0,
                        100.0,
                        public,
                    ),
                )
            character = Character(
                identity,
                name.strip(),
                playable,
                location_id,
                TextRecord(f"{identity}-appearance", "Appearance", appearance.strip(), public),
                TextRecord(f"{identity}-expression", "Expression", expression.strip(), public),
                TextRecord(f"{identity}-personality", "Personality", personality.strip(), private),
                TextRecord(f"{identity}-history", "History", history.strip(), private),
                (),
                stats,
            )
            updated = (*characters, character)
        world = replace(buffer.content.world, characters=updated)
        _set_content(buffer, replace(buffer.content, world=world))
        st.rerun()
    if selected:
        st.caption("Changes update the local buffer. Use Save draft to persist them.")
        with st.expander("Author-only knowledge and stats"):
            st.caption(
                "Preview only; updating this form preserves these records and visibility settings."
            )
            for record in selected.knowledge:
                st.markdown(
                    f"**{record.title}** ({record.access.visibility.value}; {record.stance})"
                )
                st.write(record.text)
            if not selected.knowledge:
                st.write("No additional knowledge records.")
            st.dataframe(
                [
                    {
                        "Stat": stat.name,
                        "Value": stat.value,
                        "Minimum": stat.minimum,
                        "Maximum": stat.maximum,
                        "Visibility": stat.access.visibility.value,
                    }
                    for stat in selected.stats
                ],
                hide_index=True,
                width="stretch",
            )
    if characters:
        remove_id = st.selectbox(
            "Remove character",
            [character.id for character in characters],
            format_func=lambda identity: next(c.name for c in characters if c.id == identity),
            key=f"character_remove_{buffer.local_revision}",
        )
        if st.button(
            "Remove selected character", key=f"character_remove_button_{buffer.local_revision}"
        ):
            world = replace(
                buffer.content.world,
                characters=tuple(c for c in characters if c.id != remove_id),
            )
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()


def _locations_tab(buffer: EditorBuffer) -> None:
    locations = buffer.content.world.locations
    options = [None, *(location.id for location in locations)]
    selected_id = st.selectbox(
        "Location",
        options,
        format_func=lambda identity: (
            "Add a location"
            if identity is None
            else next(location.name for location in locations if location.id == identity)
        ),
        key=f"editor_location_{buffer.campaign_id}_{buffer.local_revision}",
    )
    selected = next((location for location in locations if location.id == selected_id), None)
    with st.form(f"location_form_{buffer.campaign_id}_{buffer.local_revision}"):
        identity = st.text_input("Location ID", value=selected.id if selected else "")
        name = st.text_input("Location name", value=selected.name if selected else "")
        description = st.text_area(
            "Location description",
            value=selected.description.text if selected else "",
        )
        connected = st.text_input(
            "Connected location IDs, comma separated",
            value=", ".join(selected.connected_location_ids) if selected else "",
        )
        apply_location = st.form_submit_button(
            "Update location" if selected else "Add location", type="primary"
        )
    if apply_location:
        links = tuple(value.strip() for value in connected.split(",") if value.strip())
        if selected is None:
            record = Location(
                identity.strip(),
                name.strip(),
                TextRecord(
                    f"{identity.strip()}-description",
                    f"{name.strip()} Description",
                    description,
                    Access(Visibility.PUBLIC),
                ),
                links,
            )
            updated_locations = (*locations, record)
        else:
            record = replace(
                selected,
                id=identity.strip(),
                name=name.strip(),
                description=replace(selected.description, text=description),
                connected_location_ids=links,
            )
            updated_locations = tuple(
                record if location.id == selected.id else location for location in locations
            )
        world = replace(buffer.content.world, locations=updated_locations)
        _set_content(buffer, replace(buffer.content, world=world))
        st.rerun()
    if selected is not None and st.button(
        "Remove selected location",
        key=f"remove_location_{buffer.campaign_id}_{buffer.local_revision}",
    ):
        world = replace(
            buffer.content.world,
            locations=tuple(location for location in locations if location.id != selected.id),
        )
        _set_content(buffer, replace(buffer.content, world=world))
        st.rerun()


def _items_tab(buffer: EditorBuffer) -> None:
    templates = buffer.content.world.item_templates
    items = buffer.content.world.items
    st.markdown("**Item templates**")
    st.dataframe(
        [
            {
                "ID": template.id,
                "Name": template.name,
                "Maximum/session": template.max_instances_per_session,
            }
            for template in templates
        ],
        hide_index=True,
        width="stretch",
    )
    with st.form(f"template_add_{buffer.campaign_id}_{buffer.local_revision}"):
        name = st.text_input("Template name")
        description = st.text_area("Template description")
        maximum = st.number_input("Maximum instances per session", 1, 100, 1)
        add_template = st.form_submit_button("Add item template", type="primary")
    if add_template:
        identity = f"template-{uuid4().hex[:12]}"
        template = ItemTemplate(
            identity,
            name.strip(),
            TextRecord(
                f"{identity}-description",
                f"{name.strip()} description",
                description.strip(),
                Access(Visibility.PUBLIC),
            ),
            None,
            maximum,
        )
        world = replace(buffer.content.world, item_templates=(*templates, template))
        _set_content(buffer, replace(buffer.content, world=world))
        st.rerun()
    destinations = [
        *(("location", location.id, location.name) for location in buffer.content.world.locations),
        *(
            ("holder", character.id, character.name)
            for character in buffer.content.world.characters
        ),
    ]
    if templates and destinations:
        with st.form(f"item_add_{buffer.campaign_id}_{buffer.local_revision}"):
            template_id = st.selectbox(
                "Template",
                [template.id for template in templates],
                format_func=lambda identity: next(t.name for t in templates if t.id == identity),
            )
            destination = st.selectbox(
                "Initial placement",
                destinations,
                format_func=lambda value: f"{value[0].title()}: {value[2]}",
            )
            add_item = st.form_submit_button("Add initial item")
        if add_item:
            item = ItemInstance(
                f"item-{uuid4().hex[:12]}",
                template_id,
                destination[1] if destination[0] == "holder" else None,
                destination[1] if destination[0] == "location" else None,
                Access(Visibility.PUBLIC),
            )
            world = replace(buffer.content.world, items=(*items, item))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    removable = [("template", value.id) for value in templates] + [
        ("item", value.id) for value in items
    ]
    if removable:
        selected = st.selectbox(
            "Remove item record",
            removable,
            format_func=lambda value: f"{value[0].title()}: {value[1]}",
            key=f"item_remove_{buffer.local_revision}",
        )
        if st.button(
            "Remove selected item record", key=f"item_remove_button_{buffer.local_revision}"
        ):
            world = buffer.content.world
            if selected[0] == "template":
                world = replace(
                    world,
                    item_templates=tuple(t for t in templates if t.id != selected[1]),
                    items=tuple(i for i in items if i.template_id != selected[1]),
                )
            else:
                world = replace(world, items=tuple(i for i in items if i.id != selected[1]))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    st.markdown("**Initial item instances**")
    st.dataframe(
        [
            {
                "ID": item.id,
                "Template": item.template_id,
                "Holder": item.holder_id,
                "Location": item.location_id,
                "Visibility": item.access.visibility.value,
            }
            for item in items
        ],
        hide_index=True,
        width="stretch",
    )


def _relationships_tab(buffer: EditorBuffer) -> None:
    relationships = buffer.content.world.relationships
    st.dataframe(
        [
            {
                "ID": relation.id,
                "Source": relation.source_id,
                "Target": relation.target_id,
                "Label": relation.label,
                "Description": relation.description.text,
            }
            for relation in relationships
        ],
        hide_index=True,
        width="stretch",
    )
    characters = buffer.content.world.characters
    if len(characters) >= 2:
        with st.form(f"relationship_add_{buffer.campaign_id}_{buffer.local_revision}"):
            source = st.selectbox(
                "Source character",
                [character.id for character in characters],
                format_func=lambda identity: next(c.name for c in characters if c.id == identity),
            )
            target = st.selectbox(
                "Target character",
                [character.id for character in characters],
                format_func=lambda identity: next(c.name for c in characters if c.id == identity),
            )
            label = st.text_input("Relationship label")
            description = st.text_area("Relationship description")
            add = st.form_submit_button("Add relationship", type="primary")
        if add:
            identity = f"relationship-{uuid4().hex[:12]}"
            relationship = Relationship(
                identity,
                source,
                target,
                label.strip(),
                TextRecord(
                    f"{identity}-description",
                    label.strip(),
                    description.strip(),
                    Access(Visibility.PUBLIC),
                ),
                (),
            )
            world = replace(buffer.content.world, relationships=(*relationships, relationship))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    else:
        st.info("Add two characters before defining relationships.")
    if relationships:
        remove_id = st.selectbox(
            "Remove relationship",
            [relation.id for relation in relationships],
            format_func=lambda identity: next(r.label for r in relationships if r.id == identity),
            key=f"relationship_remove_{buffer.local_revision}",
        )
        if st.button(
            "Remove selected relationship",
            key=f"relationship_remove_button_{buffer.local_revision}",
        ):
            world = replace(
                buffer.content.world,
                relationships=tuple(r for r in relationships if r.id != remove_id),
            )
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()


def _rules_tab(buffer: EditorBuffer) -> None:
    world = buffer.content.world
    st.markdown("**Narrative rules**")
    for rule in world.narrative_rules:
        st.markdown(f"- **{rule.title}:** {rule.text}")
    st.markdown("**Flags and stat bands**")
    st.json(
        {
            "flags": json_value(world.flags),
            "stat_bands": json_value(world.stat_bands),
        },
        expanded=False,
    )
    with st.form(f"rule_add_{buffer.campaign_id}_{buffer.local_revision}"):
        title = st.text_input("Rule title")
        text = st.text_area("Rule text")
        add_rule = st.form_submit_button("Add narrative rule", type="primary")
    if add_rule:
        identity = f"rule-{uuid4().hex[:12]}"
        rule = TextRecord(identity, title.strip(), text.strip(), Access(Visibility.DIRECTOR))
        world = replace(world, narrative_rules=(*world.narrative_rules, rule))
        _set_content(buffer, replace(buffer.content, world=world))
        st.rerun()
    targets = [
        *((ConditionKind.AT_LOCATION, location.id, location.name) for location in world.locations),
        *((ConditionKind.OWNS_ITEM, item.id, item.id) for item in world.items),
    ]
    if targets:
        with st.form(f"flag_add_{buffer.campaign_id}_{buffer.local_revision}"):
            label = st.text_input("Flag label")
            target = st.selectbox(
                "Condition that permits setting it",
                targets,
                format_func=lambda value: f"{value[0].value}: {value[2]}",
            )
            initial = st.checkbox("Initially enabled")
            add_flag = st.form_submit_button("Add flag")
        if add_flag:
            identity = f"flag-{uuid4().hex[:12]}"
            flag = FlagDefinition(
                identity,
                label.strip(),
                initial,
                Condition(target[0], target[1], True),
                Access(Visibility.PUBLIC),
            )
            world = replace(world, flags=(*world.flags, flag))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    stats = tuple(
        stat for owner in (*world.characters, *world.relationships) for stat in owner.stats
    )
    if stats:
        with st.form(f"band_add_{buffer.campaign_id}_{buffer.local_revision}"):
            stat_id = st.selectbox(
                "Stat for descriptive band",
                [stat.id for stat in stats],
                format_func=lambda identity: next(s.name for s in stats if s.id == identity),
            )
            selected_stat = next(stat for stat in stats if stat.id == stat_id)
            minimum = st.number_input("Band minimum", value=float(selected_stat.minimum))
            maximum = st.number_input("Band maximum", value=float(selected_stat.maximum))
            band_text = st.text_input("Band description")
            add_band = st.form_submit_button("Add stat band")
        if add_band:
            world = replace(
                world,
                stat_bands=(
                    *world.stat_bands,
                    StatBand(stat_id, minimum, maximum, band_text.strip()),
                ),
            )
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    removable = [("rule", value.id) for value in world.narrative_rules] + [
        ("flag", value.id) for value in world.flags
    ]
    if removable:
        selected = st.selectbox(
            "Remove rule or flag",
            removable,
            format_func=lambda value: f"{value[0].title()}: {value[1]}",
            key=f"rule_remove_{buffer.local_revision}",
        )
        if st.button("Remove selected rule", key=f"rule_remove_button_{buffer.local_revision}"):
            if selected[0] == "rule":
                world = replace(
                    world,
                    narrative_rules=tuple(r for r in world.narrative_rules if r.id != selected[1]),
                )
            else:
                world = replace(world, flags=tuple(f for f in world.flags if f.id != selected[1]))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    if world.stat_bands:
        band_index = st.selectbox(
            "Remove stat band",
            range(len(world.stat_bands)),
            format_func=lambda index: (
                f"{world.stat_bands[index].stat_id}: {world.stat_bands[index].text}"
            ),
            key=f"band_remove_{buffer.local_revision}",
        )
        if st.button("Remove selected band", key=f"band_remove_button_{buffer.local_revision}"):
            world = replace(
                world,
                stat_bands=tuple(
                    band for index, band in enumerate(world.stat_bands) if index != band_index
                ),
            )
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()


def _milestones_tab(buffer: EditorBuffer) -> None:
    milestones = buffer.content.world.milestones
    st.dataframe(
        [
            {
                "ID": marker.id,
                "Label": marker.label,
                "Condition": marker.condition.kind.value,
                "Target": marker.condition.target_id,
            }
            for marker in milestones
        ],
        hide_index=True,
        width="stretch",
    )
    world = buffer.content.world
    targets = [
        *((ConditionKind.AT_LOCATION, location.id, location.name) for location in world.locations),
        *((ConditionKind.OWNS_ITEM, item.id, item.id) for item in world.items),
        *((ConditionKind.FLAG_EQUALS, flag.id, flag.label) for flag in world.flags),
    ]
    if targets:
        with st.form(f"milestone_add_{buffer.campaign_id}_{buffer.local_revision}"):
            label = st.text_input("Milestone label")
            target = st.selectbox(
                "Achievement condition",
                targets,
                format_func=lambda value: f"{value[0].value}: {value[2]}",
            )
            add = st.form_submit_button("Add milestone", type="primary")
        if add:
            identity = f"milestone-{uuid4().hex[:12]}"
            condition = Condition(target[0], target[1], True)
            fingerprint = sha256(
                f"{condition.kind.value}:{condition.target_id}:{condition.expected}".encode()
            ).hexdigest()
            marker = MilestoneDefinition(identity, label.strip(), condition, fingerprint)
            world = replace(world, milestones=(*milestones, marker))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()
    else:
        st.info("Add a location, item, or flag before defining a milestone.")
    if milestones:
        remove_id = st.selectbox(
            "Remove milestone",
            [marker.id for marker in milestones],
            format_func=lambda identity: next(m.label for m in milestones if m.id == identity),
            key=f"milestone_remove_{buffer.local_revision}",
        )
        if st.button(
            "Remove selected milestone", key=f"milestone_remove_button_{buffer.local_revision}"
        ):
            world = replace(world, milestones=tuple(m for m in milestones if m.id != remove_id))
            _set_content(buffer, replace(buffer.content, world=world))
            st.rerun()


def _advanced_editor(buffer: EditorBuffer) -> None:
    with st.expander("Advanced structured campaign editor"):
        st.caption(
            "Edit the complete typed campaign document. Invalid or unknown fields are rejected."
        )
        key = f"editor_json_{buffer.campaign_id}_{buffer.local_revision}"
        text = st.text_area(
            "Campaign JSON",
            value=json.dumps(json_value(buffer.content), indent=2, ensure_ascii=False),
            height=420,
            key=key,
        )
        if st.button("Apply structured document", key=f"apply_{key}"):
            try:
                content = decode(load_json(text), CampaignContent)
            except (TypeError, ValueError) as error:
                st.error(f"Invalid campaign document: {error}")
            else:
                _set_content(buffer, content)
                st.rerun()


def _coeditor_tab(ui: UIContext, buffer: EditorBuffer) -> None:
    selected_ids = st.multiselect(
        "Records the AI may edit",
        _record_ids(buffer.content),
        key=f"coeditor_scope_{buffer.campaign_id}",
    )
    with st.form(f"coeditor_request_{buffer.campaign_id}", clear_on_submit=True):
        instruction = st.text_area("Instruction to AI coeditor", max_chars=8000)
        request = st.form_submit_button("Request proposal", type="primary")
    if request:
        submit(
            ui,
            "editor.proposal",
            RequestEditorProposal(buffer, instruction, tuple(selected_ids)),
        )
    proposed = consume_completed(ui, "editor.proposal")
    if isinstance(proposed, EditorProposal):
        st.session_state[PROPOSAL_KEY] = proposed

    proposal = st.session_state.get(PROPOSAL_KEY)
    if not isinstance(proposal, EditorProposal):
        st.caption("Select records and request a proposal. The AI cannot save or publish.")
        return
    st.markdown(f"**Proposal:** {proposal.summary}")
    st.dataframe(
        [
            {
                "Record": change.record_id,
                "Field": change.path,
                "Before": change.before,
                "After": change.after,
            }
            for change in proposal.changes
        ],
        hide_index=True,
        width="stretch",
    )
    accept_col, reject_col = st.columns(2)
    with accept_col:
        if st.button("Accept into buffer", type="primary", width="stretch"):
            submit(
                ui,
                "editor.review",
                ReviewEditorProposal(proposal.id, buffer, True),
            )
    with reject_col:
        if st.button("Reject proposal", width="stretch"):
            submit(
                ui,
                "editor.review",
                ReviewEditorProposal(proposal.id, buffer, False),
            )
    reviewed = consume_completed(ui, "editor.review")
    if isinstance(reviewed, EditorBuffer):
        st.session_state[BUFFER_KEY] = reviewed
        st.session_state.pop(PROPOSAL_KEY, None)
        st.rerun()


def _save_publish_controls(ui: UIContext, buffer: EditorBuffer) -> None:
    dirty = buffer.local_revision > 0
    if dirty:
        st.warning("This campaign has unsaved local changes.")
    save_col, readiness_col, publish_col = st.columns(3)
    with save_col:
        if st.button("Save draft", type="primary", width="stretch", disabled=not dirty):
            submit(
                ui,
                "editor.save",
                SaveDraft(buffer.campaign_id, buffer.persisted_revision, buffer.content),
            )
    saved = consume_completed(ui, "editor.save")
    if isinstance(saved, CampaignDraft):
        st.session_state[BUFFER_KEY] = EditorBuffer(saved.id, saved.revision, 0, saved.content)
        st.rerun()

    with readiness_col:
        check = st.button(
            "Check readiness",
            width="stretch",
            disabled=dirty,
            help="Save local changes before checking readiness." if dirty else None,
        )
    if check:
        issues = query(ui, Query(QueryKind.CAMPAIGN_READINESS, buffer.campaign_id))
        st.session_state["ui.editor.readiness"] = issues
    issues = st.session_state.get("ui.editor.readiness")
    if issues == ():
        st.success("The saved draft is structurally ready to publish.")
    elif issues:
        st.warning("The saved draft needs attention before publication.")
        st.dataframe(
            [
                {"Path": issue.path, "Code": issue.code, "Message": issue.message}
                for issue in issues
            ],
            hide_index=True,
            width="stretch",
        )

    with publish_col:
        if st.button("Publish version", width="stretch", disabled=dirty):
            submit(
                ui,
                "editor.publish",
                PublishCampaign(buffer.campaign_id, buffer.persisted_revision),
            )
    published = consume_completed(ui, "editor.publish")
    if published is not None:
        st.success(f"Published {published.title}, version {published.version}.")


def editor(ui: UIContext) -> None:
    st.header("Campaign workspace")
    if require_account(ui) is None:
        return

    with st.expander("Create campaign", expanded=False):
        with st.form("create_campaign_form", clear_on_submit=True):
            title = st.text_input("Campaign title")
            summary = st.text_area("Initial summary")
            create = st.form_submit_button("Create draft", type="primary")
        if create:
            submit(ui, "editor.create", CreateCampaign(title, summary))
        created = consume_completed(ui, "editor.create")
        if isinstance(created, CampaignDraft):
            st.session_state[CAMPAIGN_KEY] = created.id
            st.session_state[BUFFER_KEY] = EditorBuffer(
                created.id, created.revision, 0, created.content
            )
            st.rerun()

    campaigns = query(ui, Query(QueryKind.MY_CAMPAIGNS))
    if campaigns is None:
        return
    if not campaigns:
        st.info("Create your first campaign draft.")
        return
    campaign_ids = [campaign.id for campaign in campaigns]
    if st.session_state.get(CAMPAIGN_KEY) not in campaign_ids:
        st.session_state[CAMPAIGN_KEY] = campaign_ids[0]
    campaign_id = st.selectbox(
        "Campaign draft",
        campaign_ids,
        format_func=lambda identity: next(
            campaign.title for campaign in campaigns if campaign.id == identity
        ),
        key=CAMPAIGN_KEY,
    )
    buffer = st.session_state.get(BUFFER_KEY)
    if not isinstance(buffer, EditorBuffer) or buffer.campaign_id != campaign_id:
        buffer = _load_buffer(ui, campaign_id)
    if not isinstance(buffer, EditorBuffer):
        return

    st.caption(
        f"Saved revision {buffer.persisted_revision} · local revision {buffer.local_revision}"
    )
    tabs = st.tabs(
        (
            "Setup",
            "Canon & knowledge",
            "Characters",
            "Locations",
            "Items",
            "Relationships",
            "Rules & stats",
            "Milestones",
            "AI coeditor",
        )
    )
    with tabs[0]:
        _setup_tab(buffer)
    with tabs[1]:
        _canon_tab(buffer)
    with tabs[2]:
        _characters_tab(buffer)
    with tabs[3]:
        _locations_tab(buffer)
    with tabs[4]:
        _items_tab(buffer)
    with tabs[5]:
        _relationships_tab(buffer)
    with tabs[6]:
        _rules_tab(buffer)
    with tabs[7]:
        _milestones_tab(buffer)
    with tabs[8]:
        _coeditor_tab(ui, buffer)

    _advanced_editor(buffer)
    st.divider()
    _save_publish_controls(ui, buffer)
