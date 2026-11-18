from ..models.common import Condition, ConditionKind
from ..models.gameplay import SessionSnapshot


def condition_met(condition: Condition | None, session: SessionSnapshot) -> bool:
    if condition is None:
        return False

    pc = next(
        (c for c in session.world.characters if c.id == session.player_character_id),
        None,
    )
    if pc is None:
        return False

    if condition.kind == ConditionKind.AT_LOCATION:
        if not any(loc.id == condition.target_id for loc in session.world.locations):
            return False
        actual = pc.location_id == condition.target_id

    elif condition.kind == ConditionKind.OWNS_ITEM:
        item = next(
            (item for item in session.world.items if item.id == condition.target_id),
            None,
        )
        if item is None:
            return False
        actual = item.holder_id == pc.id

    elif condition.kind == ConditionKind.FLAG_EQUALS:
        values = dict(session.flag_values)
        if condition.target_id not in values:
            return False
        actual = values[condition.target_id]

    else:
        return False

    return actual == condition.expected
