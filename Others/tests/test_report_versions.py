import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.codec import decode, load_json
from shared.commands import RetryTurn
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.world import CampaignDraft


class ReportVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteStore(Path(temporary.name) / "report.sqlite3")
        self.store.initialize()
        self.owner = self.store.create_account("Alice", "test-hash").id
        fixture = Path(__file__).resolve().parents[1] / "fixtures/thornmere.json"
        content = decode(load_json(fixture.read_text())["draft"], CampaignDraft).content
        draft = self.store.create_campaign(self.owner, "Story", "Summary")
        self.draft = self.store.save_draft(self.owner, draft.id, 0, content)
        self.v1 = self.store.publish(self.owner, draft.id, self.draft.revision)
        self.state = self.store.start_session(self.owner, self.v1, "rowan")

    def failure(self, operation, code):
        self.store.admit_turn(
            self.owner, operation, operation, self.state.id, self.state.revision, operation
        )
        self.store.record_attempt_failure(self.owner, operation, self.state.id, code, False)

    def old_activity_and_update(self):
        self.store.admit_turn(self.owner, "move", "move", self.state.id, 0, "Move")
        world = replace(
            self.state.world,
            characters=tuple(
                replace(c, location_id="antechamber") if c.id == "rowan" else c
                for c in self.state.world.characters
            ),
        )
        committed = self.store.commit_turn(
            self.owner, "move", 0, replace(self.state, world=world), "Move", "You move."
        )
        self.store.apply_committed_notice(committed.notice)
        self.state = self.store.load_session(self.owner, self.state.id)
        self.failure("old-rejected", "invalid")
        self.failure("old-failed", "provider_failed")
        previous = self.store.campaign_report(self.owner, self.draft.id, self.v1.id)
        draft = self.store.save_draft(
            self.owner,
            self.draft.id,
            self.draft.revision,
            replace(self.draft.content, title="Updated title"),
        )
        self.v2 = self.store.publish(self.owner, draft.id, draft.revision)
        self.state = self.store.adopt_update(
            self.owner,
            "adopt",
            self.state.revision,
            replace(self.state, base_version_id=self.v2.id),
        )
        return previous

    def totals(self, version=None):
        report = self.store.campaign_report(self.owner, self.draft.id, version)
        return (
            report.started_playthroughs,
            report.accepted_turns,
            report.rejected_turns,
            report.failed_turns,
        )

    def test_adoption_preserves_old_counts_and_retry_uses_its_current_version(self):
        previous = self.old_activity_and_update()
        self.assertEqual(
            self.store.campaign_report(self.owner, self.draft.id, self.v1.id), previous
        )
        self.assertEqual(self.totals(self.v1.id), (1, 1, 1, 1))
        self.assertEqual(self.totals(self.v2.id), (1, 0, 0, 0))
        visited = next(m for m in previous.milestones if m.marker_id == "entered-antechamber")
        self.assertEqual((visited.playthrough_count, visited.eligible_playthroughs), (1, 1))
        self.assertEqual((visited.distinct_account_count, visited.eligible_accounts), (1, 1))

        retry = self.store.retry_turn(
            self.owner, RetryTurn("old-failed", self.state.id, 1), "retry", "retry"
        )
        self.store.commit_turn(self.owner, "retry", 1, self.state, retry.player_input, "You wait.")
        self.state = self.store.load_session(self.owner, self.state.id)
        self.failure("new-rejected", "invalid")
        self.failure("new-failed", "provider_failed")
        self.assertEqual(self.totals(self.v1.id), (1, 1, 1, 1))
        self.assertEqual(self.totals(self.v2.id), (1, 1, 1, 1))
        self.assertEqual(self.totals(), (1, 2, 2, 2))
        self.store = SQLiteStore(self.store.database_path)
        self.store.initialize()
        self.assertEqual(self.totals(self.v1.id), (1, 1, 1, 1))
        self.assertEqual(self.totals(self.v2.id), (1, 1, 1, 1))

    def make_v2(self):
        with closing(self.store._connect()) as db:
            db.execute("ALTER TABLE turn_attempts DROP COLUMN base_version_id")
            db.execute("DELETE FROM schema_versions")
            db.execute("INSERT INTO schema_versions VALUES (2, 'old')")
            db.execute("PRAGMA user_version = 2")
            db.commit()

    def test_upgrade_restores_known_history_without_guessing_old_failed_versions(self):
        self.old_activity_and_update()
        second = self.store.start_session(self.owner, self.v1, "rowan")
        self.store.admit_turn(self.owner, "single-version", "single", second.id, 0, "Wait")
        self.store.record_attempt_failure(self.owner, "single-version", second.id, "invalid", False)
        self.make_v2()
        self.store.initialize()
        self.store.initialize()
        with closing(self.store._connect()) as db:
            versions = dict(db.execute("SELECT operation_id, base_version_id FROM turn_attempts"))
        self.assertEqual(versions["move"], self.v1.id)
        self.assertEqual(versions["single-version"], self.v1.id)
        self.assertIsNone(versions["old-rejected"])
        self.assertIsNone(versions["old-failed"])
        self.assertEqual(self.totals(self.v1.id), (2, 1, 1, 0))
        self.assertEqual(self.totals(self.v2.id), (1, 0, 0, 0))
        self.assertEqual(self.totals(), (2, 1, 2, 1))
        self.assertEqual(self.store.load_session(self.owner, self.state.id), self.state)

    def test_failed_v2_upgrade_preserves_schema_and_data(self):
        self.old_activity_and_update()
        self.make_v2()
        with closing(self.store._connect()) as db:
            db.execute("""CREATE TRIGGER reject_upgrade BEFORE INSERT ON schema_versions
                          BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.initialize()
        with closing(self.store._connect()) as db:
            self.assertNotIn(
                "base_version_id", {r[1] for r in db.execute("PRAGMA table_info(turn_attempts)")}
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turn_attempts").fetchone()[0], 3)
        self.assertEqual(self.store.load_session(self.owner, self.state.id), self.state)
