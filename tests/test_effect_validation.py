import unittest
from dataclasses import asdict, replace
from pathlib import Path

from shared.codec import decode, load_json
from shared.domain.gameplay import GameplayService
from shared.domain.knowledge import KnowledgePolicy
from shared.errors import ApplicationError, ErrorCode
from shared.models.gameplay import Effect, EffectKind, SessionSnapshot, TurnProposal
from shared.models.world import CampaignDraft


class EffectValidationTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        draft = decode(load_json(path.read_text(encoding="utf-8"))["draft"], CampaignDraft)
        self.state = SessionSnapshot(
            id="session",
            owner_id="owner",
            campaign_id=draft.id,
            base_version_id="version",
            revision=0,
            player_character_id="rowan",
            world=draft.content.world,
            disclosed_ids=(),
            flag_values=tuple((f.id, f.initial) for f in draft.content.world.flags),
            setup=(draft.content.background, draft.content.opening),
        )
        self.service = GameplayService(None, None)
        self.policy = KnowledgePolicy()
        self.move = Effect(
            EffectKind.MOVE_CHARACTER,
            "rowan",
            destination_id="antechamber",
        )
        self.take = Effect(
            EffectKind.TRANSFER_ITEM,
            "rowan",
            target_id="wedding-chalice",
            destination_id="rowan",
        )
        self.reveal = Effect(
            EffectKind.REQUEST_DISCLOSURE,
            "rowan",
            target_id="hidden-dagger",
        )

    def apply(self, *effects, facts=()):
        proposal = TurnProposal(0, effects, facts)
        candidate = self.service.validate_and_project(self.state, proposal)
        return self.policy.authorize_disclosures(self.state, candidate, proposal)

    def reject(self, *effects, facts=()):
        with self.assertRaises(ApplicationError) as caught:
            self.apply(*effects, facts=facts)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID)

    def test_movement_and_numeric_effects(self):
        stat = Effect(
            EffectKind.CHANGE_STAT,
            "rowan",
            target_id="rowan",
            stat_id="rowan-composure",
            delta=-10,
        )
        relation = Effect(
            EffectKind.CHANGE_RELATIONSHIP_STAT,
            "rowan",
            target_id="rowan-mara",
            stat_id="rowan-mara-trust",
            delta=5,
        )
        result = self.apply(self.move, stat, stat, relation)
        pc = next(c for c in result.world.characters if c.id == "rowan")
        self.assertEqual(pc.location_id, "antechamber")
        self.assertEqual(pc.stats[0].value, 40)
        self.assertEqual(result.world.relationships[0].stats[0].value, 55)
        self.assertEqual(result.revision, 0)

        self.reject(replace(stat, delta=1000))
        self.reject(replace(stat, delta=float("nan")))
        self.reject(replace(stat, delta=True))
        self.reject(replace(relation, actor_id="cedric"))

    def test_shape_revision_and_limits(self):
        for effect in (
            replace(self.move, destination_id="missing"),
            replace(self.move, actor_id="missing"),
            replace(self.move, destination_id=None),
            replace(self.move, target_id="rowan"),
        ):
            with self.subTest(effect=effect):
                self.reject(effect)

        self.reject(*(self.move,) * 33)
        self.reject(facts=("opening",) * 65)
        with self.assertRaises(ApplicationError) as caught:
            self.service.validate_and_project(self.state, TurnProposal(1, (), ()))
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)

    def test_item_transfer_flags_and_creation(self):
        flag = Effect(
            EffectKind.SET_FLAG,
            "rowan",
            target_id="inquiry-complete",
            enabled=True,
        )
        create = Effect(
            EffectKind.CREATE_ITEM,
            "rowan",
            template_id="seal-template",
            destination_id="rowan",
        )
        result = self.apply(self.take, flag, self.move, create)
        self.assertTrue(dict(result.flag_values)["inquiry-complete"])
        self.assertFalse(result.world.flags[0].initial)

        created = result.world.items[-1]
        self.assertEqual(created.template_id, "seal-template")
        self.assertEqual(created.holder_id, "rowan")
        self.assertIsNone(created.location_id)
        self.assertNotIn(created.id, {item.id for item in self.state.world.items})

        self.reject(flag)
        self.reject(create)
        self.reject(self.move, create, create)
        self.reject(self.take, replace(self.take, destination_id="mara"), self.take)
        self.reject(self.move, self.take)

    def test_discovery_is_ordered_and_allows_pickup(self):
        pickup = replace(self.take, target_id="hidden-dagger")
        result = self.apply(self.move, self.reveal, pickup, facts=("hidden-dagger",))
        self.assertEqual(result.disclosed_ids, ("hidden-dagger",))
        dagger = next(i for i in result.world.items if i.id == "hidden-dagger")
        self.assertEqual(dagger.holder_id, "rowan")
        self.assertIsNone(dagger.location_id)

        self.reject(self.reveal, self.move)
        self.reject(self.move, pickup, self.reveal)
        self.reject(self.move, replace(self.reveal, actor_id="mara"))

    def test_private_facts_stay_private(self):
        self.assertEqual(self.apply(facts=("opening",)), self.state)

        for target in ("cedric-private-secret", "missing", "rowan-composure"):
            self.reject(replace(self.reveal, target_id=target))
        for fact in ("cedric-private-secret", "tomas-private-report", "missing"):
            self.reject(facts=(fact,))

        view = self.policy.player_view(self.state, (), "")
        self.assertNotIn("SPOILER-CANARY", repr(view))
        self.reject(
            self.move,
            self.reveal,
            replace(self.move, destination_id="great-hall"),
            facts=("hidden-dagger",),
        )

    def test_rejected_proposal_leaves_original_unchanged(self):
        original = asdict(self.state)
        self.reject(
            self.move,
            replace(self.move, destination_id="service-passage"),
        )
        self.assertEqual(asdict(self.state), original)
        self.assertEqual(self.apply(), self.state)
