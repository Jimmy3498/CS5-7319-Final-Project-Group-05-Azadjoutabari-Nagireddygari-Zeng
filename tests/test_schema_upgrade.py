import json
import re
import sqlite3
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.sqlite import SQLiteStore

SCHEMA = Path(__file__).resolve().parents[1] / "shared/infrastructure/schema.sql"
OLD_EDITOR_TABLE = """CREATE TABLE editor_proposals (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
 campaign_id TEXT NOT NULL REFERENCES campaigns(id), base_persisted_revision INTEGER NOT NULL,
 base_buffer_revision INTEGER NOT NULL, candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
 status TEXT NOT NULL, created_at TEXT NOT NULL
);"""


class SchemaUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteStore(Path(temporary.name) / "demo.sqlite3")

    def seed_v1(self, current_columns=False):
        schema = SCHEMA.read_text()
        if not current_columns:
            schema, count = re.subn(
                r"CREATE TABLE editor_proposals \([\s\S]*?\n\);",
                OLD_EDITOR_TABLE,
                schema,
            )
            self.assertEqual(count, 1)
        with closing(sqlite3.connect(self.store.database_path)) as db:
            db.executescript(schema)
            db.execute("INSERT INTO schema_versions VALUES (1, 'original-date')")
            db.execute("PRAGMA user_version = 1")
            db.commit()
        account = self.store.create_account("Alice", "test-hash")
        draft = self.store.create_campaign(account.id, "Keep this story", "Summary")
        with closing(self.store._connect()) as db:
            columns = [
                "id",
                "owner_id",
                "campaign_id",
                "base_persisted_revision",
                "base_buffer_revision",
                "candidate_json",
                "status",
                "created_at",
            ]
            values = [
                "proposal",
                account.id,
                draft.id,
                0,
                0,
                json.dumps(asdict(draft.content)),
                "pending",
                "original-date",
            ]
            if current_columns:
                columns += ["summary", "changes_json"]
                values += ["Original summary", "[]"]
            db.execute(
                f"INSERT INTO editor_proposals ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in values)})",
                values,
            )
            db.commit()
        return account, draft

    def test_fresh_database_is_v2_and_repeat_initialization_preserves_data(self):
        self.store.initialize()
        account = self.store.create_account("Alice", "test-hash")
        self.store.initialize()
        self.assertEqual(self.store.credential_record("Alice")[0], account)
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0], 1)

    def test_v1_upgrade_preserves_accounts_drafts_and_proposals(self):
        account, draft = self.seed_v1()
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.credential_record("Alice")[0], account)
        self.assertEqual(self.store.load_draft(account.id, draft.id), draft)
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT * FROM editor_proposals").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(json.loads(row["candidate_json"])["title"], draft.content.title)
            self.assertEqual(row["summary"], "")
            self.assertEqual(row["changes_json"], "[]")
            self.assertIsNone(row["resolved_at"])
            self.assertEqual(
                [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")],
                [1, 2],
            )

    def test_v1_with_new_columns_keeps_existing_summary(self):
        self.seed_v1(current_columns=True)
        self.store.initialize()
        with closing(self.store._connect()) as db:
            self.assertEqual(
                db.execute("SELECT summary FROM editor_proposals").fetchone()[0],
                "Original summary",
            )

    def test_failed_upgrade_rolls_back_schema_and_version(self):
        self.seed_v1()
        with closing(self.store._connect()) as db:
            db.execute("""CREATE TRIGGER fail_upgrade BEFORE INSERT ON schema_versions
                WHEN NEW.version = 2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.initialize()
        with closing(self.store._connect()) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(editor_proposals)")}
            self.assertNotIn("summary", columns)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM editor_proposals").fetchone()[0], 1)
            db.execute("DROP TRIGGER fail_upgrade")
            db.commit()
        self.store.initialize()

    def test_unknown_schema_version_is_not_modified(self):
        with closing(sqlite3.connect(self.store.database_path)) as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaises(ApplicationError) as caught:
            self.store.initialize()
        self.assertEqual(caught.exception.code, ErrorCode.STORAGE)
        with closing(sqlite3.connect(self.store.database_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
