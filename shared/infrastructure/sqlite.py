from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..codec import decode, load_json
from ..errors import ApplicationError, ErrorCode, FeatureNotImplemented
from ..models.accounts import AccountView
from ..models.ai import ProviderProfile
from ..models.common import Access, Visibility
from ..models.gameplay import (
    AcceptedTurn,
    CommittedTurnNotice,
    SessionSnapshot,
    SessionSummary,
    TurnAdmission,
    TurnCommit,
)
from ..models.reporting import CampaignReport
from ..models.views import CampaignSummary
from ..models.world import (
    CampaignContent,
    CampaignDraft,
    CatalogEntry,
    EditorProposal,
    PublishedCampaign,
    TextRecord,
    WorldDefinition,
)


class SQLiteStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def create_account(self, username: str, password_hash: str) -> AccountView:
        account = AccountView(str(uuid4()), username.strip())

        with closing(self._connect()) as db:
            result = db.execute(
                """
                INSERT INTO accounts
                    (id, username_normalized, display_name, password_hash, created_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(username_normalized) DO NOTHING
                """,
                (account.id, account.username.casefold(), account.username, password_hash),
            )

            if result.rowcount == 0:
                raise ApplicationError(ErrorCode.CONFLICT, "Username already used")

            db.commit()

        return account

    def credential_record(self, username: str) -> tuple[AccountView, str] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT id, display_name, password_hash
                FROM accounts
                WHERE username_normalized = ?
                """,
                (username.strip().casefold(),),
            ).fetchone()

        if row is None:
            return None

        return (
            AccountView(row["id"], row["display_name"]),
            row["password_hash"],
        )

    def establish_login(self, account_id: str, client_session_id: str) -> str:
        if not client_session_id:
            raise ApplicationError(ErrorCode.INVALID, "Missing client sesison")

        session_id = str(uuid4())
        client_hash = sha256(client_session_id.encode()).hexdigest()

        # login session with 1 day expiration
        with closing(self._connect()) as db:
            db.execute(
                """
                INSERT INTO login_sessions
                    (id, account_id, client_session_hash, expires_at)
                VALUES (
                    ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+1 day')
                )
                ON CONFLICT(client_session_hash) DO UPDATE SET
                    id = excluded.id,
                    account_id = excluded.account_id,
                    expires_at = excluded.expires_at
                """,
                (session_id, account_id, client_hash),
            )
            db.commit()

        return session_id

    def resolve_login(self, client_session_id: str) -> AccountView | None:
        client_hash = sha256(client_session_id.encode()).hexdigest()

        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT a.id, a.display_name
                FROM login_sessions AS s
                JOIN accounts AS a ON a.id = s.account_id
                WHERE s.client_session_hash = ?
                AND s.expires_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (client_hash,),
            ).fetchone()

        if row is None:
            return None

        return AccountView(row["id"], row["display_name"])

    def end_login(self, client_session_id: str) -> None:
        client_hash = sha256(client_session_id.encode()).hexdigest()

        with closing(self._connect()) as db:
            db.execute(
                "DELETE FROM login_sessions WHERE client_session_hash = ?",
                (client_hash,),
            )
            db.commit()

    def create_campaign(self, owner_id: str, title: str, summary: str) -> CampaignDraft:
        content = CampaignContent(
            title=title.strip(),
            summary=summary.strip(),
            background=TextRecord(str(uuid4()), "Background", "", Access(Visibility.DIRECTOR)),
            opening=TextRecord(str(uuid4()), "Opening", "", Access(Visibility.PUBLIC)),
            world=WorldDefinition(
                canon=(),
                characters=(),
                locations=(),
                item_templates=(),
                items=(),
                relationships=(),
                flags=(),
                narrative_rules=(),
                stat_bands=(),
                milestones=(),
            ),
        )

        draft = CampaignDraft(str(uuid4()), owner_id, 0, content)

        with closing(self._connect()) as db:
            db.execute(
                """
                INSERT INTO campaigns
                    (id, owner_id, draft_revision, draft_json, updated_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    draft.id,
                    owner_id,
                    draft.revision,
                    json.dumps(asdict(content), ensure_ascii=False, allow_nan=False),
                ),
            )
            db.commit()

        return draft

    def load_draft(self, owner_id: str, campaign_id: str) -> CampaignDraft:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT id, owner_id, draft_revision, draft_json
                FROM campaigns
                WHERE id = ? AND owner_id = ?
                """,
                (campaign_id, owner_id),
            ).fetchone()

        if row is None:
            raise ApplicationError(
                ErrorCode.FORBIDDEN,
                "Campaign not found or access denied",
            )

        content = decode(load_json(row["draft_json"]), CampaignContent)

        return CampaignDraft(
            row["id"],
            row["owner_id"],
            row["draft_revision"],
            content,
        )

    def save_draft(
        self, owner_id: str, campaign_id: str, expected_revision: int, content: CampaignContent
    ) -> CampaignDraft:
        self.load_draft(owner_id, campaign_id)

        content_json = json.dumps(asdict(content), ensure_ascii=False, allow_nan=False)

        with closing(self._connect()) as db:
            result = db.execute(
                """
                UPDATE campaigns
                SET draft_json = ?,
                    draft_revision = draft_revision + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND owner_id = ? AND draft_revision = ?
                """,
                (content_json, campaign_id, owner_id, expected_revision),
            )

            if result.rowcount == 0:
                raise ApplicationError(ErrorCode.CONFLICT, "Draft changed, reload before saving")

            db.commit()

        return CampaignDraft(campaign_id, owner_id, expected_revision + 1, content)

    def publish(self, owner_id: str, campaign_id: str, expected_revision: int) -> PublishedCampaign:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            draft = db.execute(
                """
                SELECT draft_revision, draft_json
                FROM campaigns
                WHERE id = ? AND owner_id = ?
                """,
                (campaign_id, owner_id),
            ).fetchone()

            if draft is None:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Campaign not found or access denied")

            if draft["draft_revision"] != expected_revision:
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Draft changed; reload before publishing"
                )

            version = db.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM published_versions
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()[0]

            published = PublishedCampaign(
                str(uuid4()),
                campaign_id,
                owner_id,
                version,
                decode(load_json(draft["draft_json"]), CampaignContent),
            )

            db.execute(
                """
                INSERT INTO published_versions
                    (id, campaign_id, version, content_json, published_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (published.id, campaign_id, version, draft["draft_json"]),
            )
            db.commit()

        return published

    def published(self, version_id: str) -> PublishedCampaign:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT p.id, p.campaign_id, c.owner_id, p.version, p.content_json
                FROM published_versions AS p
                JOIN campaigns AS c ON c.id = p.campaign_id
                WHERE p.id = ?
                """,
                (version_id,),
            ).fetchone()

        if row is None:
            raise ApplicationError(ErrorCode.INVALID, "Published campaign not found")

        return PublishedCampaign(
            row["id"],
            row["campaign_id"],
            row["owner_id"],
            row["version"],
            decode(load_json(row["content_json"]), CampaignContent),
        )

    def catalog(self) -> tuple[CatalogEntry, ...]:
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT p.id, p.campaign_id, p.version, p.content_json
                FROM published_versions AS p
                WHERE p.version = (
                    SELECT MAX(v.version)
                    FROM published_versions AS v
                    WHERE v.campaign_id = p.campaign_id
                )
                ORDER BY p.published_at DESC, p.id
                """).fetchall()

        entries = []
        for row in rows:
            content = decode(load_json(row["content_json"]), CampaignContent)
            entries.append(
                CatalogEntry(
                    campaign_id=row["campaign_id"],
                    published_version_id=row["id"],
                    title=content.title,
                    summary=content.summary,
                    version=row["version"],
                    playable_character_names=tuple(
                        character.name
                        for character in content.world.characters
                        if character.playable
                    ),
                )
            )

        return tuple(entries)

    def start_session(
        self, owner_id: str, campaign: PublishedCampaign, pc_id: str
    ) -> SessionSnapshot:
        state = SessionSnapshot(
            id=str(uuid4()),
            owner_id=owner_id,
            campaign_id=campaign.campaign_id,
            base_version_id=campaign.id,
            revision=0,
            player_character_id=pc_id,
            world=campaign.content.world,
            disclosed_ids=(),
            flag_values=tuple((flag.id, flag.initial) for flag in campaign.content.world.flags),
            setup=(campaign.content.background, campaign.content.opening),
        )
        encoded = json.dumps(asdict(state), ensure_ascii=False, allow_nan=False)

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            db.execute(
                """
                INSERT INTO playthroughs (
                    id, owner_id, campaign_id, base_version_id,
                    player_character_id, revision, state_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    state.id,
                    owner_id,
                    campaign.campaign_id,
                    campaign.id,
                    pc_id,
                    encoded,
                ),
            )
            db.execute(
                """
                INSERT INTO session_version_memberships (
                    session_id, version_id, entered_at_revision, entered_at
                )
                VALUES (?, ?, 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (state.id, campaign.id),
            )
            db.execute(
                """
                INSERT INTO session_checkpoints (
                    id, session_id, revision, base_version_id,
                    reason, world_json, created_at
                )
                VALUES (?, ?, 0, ?, 'start', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (str(uuid4()), state.id, campaign.id, encoded),
            )
            db.commit()

        return state

    def load_session(self, owner_id: str, session_id: str) -> SessionSnapshot:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT state_json
                FROM playthroughs
                WHERE id = ? AND owner_id = ?
                """,
                (session_id, owner_id),
            ).fetchone()

        if row is None:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Session not found or access denied")

        return decode(load_json(row["state_json"]), SessionSnapshot)

    def commit_turn(
        self,
        owner_id: str,
        operation_id: str,
        expected_revision: int,
        candidate: SessionSnapshot,
        player_input: str,
        narration: str,
    ) -> TurnCommit:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            row = db.execute(
                """
                SELECT state_json FROM playthroughs
                WHERE id = ? AND owner_id = ?
                """,
                (candidate.id, owner_id),
            ).fetchone()
            if row is None:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Session not found or access denied")
            before = decode(load_json(row["state_json"]), SessionSnapshot)

            attempt = db.execute(
                """
                SELECT r.status, r.result_json, t.state,
                       t.session_id, t.expected_revision, t.player_input
                FROM operation_receipts AS r
                JOIN turn_attempts AS t ON t.operation_id = r.operation_id
                WHERE r.operation_id = ?
                  AND r.owner_id = ?
                  AND r.kind = 'turn'
                """,
                (operation_id, owner_id),
            ).fetchone()
            if (
                attempt is None
                or attempt["session_id"] != candidate.id
                or attempt["expected_revision"] != expected_revision
                or attempt["player_input"] != player_input
            ):
                raise ApplicationError(ErrorCode.CONFLICT, "Commit does not match an admitted turn")

            if attempt["status"] == "completed":
                return decode(load_json(attempt["result_json"]), TurnCommit)

            if attempt["status"] != "running" or attempt["state"] != "running":
                raise ApplicationError(ErrorCode.CONFLICT, "Turn is no longer running")

            identity = (
                "id",
                "owner_id",
                "campaign_id",
                "base_version_id",
                "player_character_id",
                "setup",
            )
            if any(getattr(before, name) != getattr(candidate, name) for name in identity):
                raise ApplicationError(ErrorCode.INVALID, "Session identity changed")
            if before.revision != expected_revision or candidate.revision != expected_revision:
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Session changed; reload before committing"
                )
            if not narration.strip():
                raise ApplicationError(ErrorCode.INVALID, "Narration is empty")

            saved = replace(candidate, revision=expected_revision + 1)
            encoded = json.dumps(asdict(saved), ensure_ascii=False, allow_nan=False)
            decode(load_json(encoded), SessionSnapshot)

            turn = AcceptedTurn(
                str(uuid4()),
                saved.id,
                operation_id,
                saved.revision,
                player_input,
                narration,
            )
            notice = CommittedTurnNotice(
                str(uuid4()),
                operation_id,
                saved.id,
                saved.campaign_id,
                saved.base_version_id,
                saved.revision,
            )
            result = TurnCommit(turn, notice)
            result_json = json.dumps(asdict(result), ensure_ascii=False, allow_nan=False)
            load_json(result_json)
            now = db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')").fetchone()[0]

            db.execute(
                """
                UPDATE playthroughs
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (saved.revision, encoded, now, saved.id, owner_id),
            )
            db.execute(
                """
                INSERT INTO accepted_turns (
                    id, session_id, operation_id, revision,
                    player_input, public_narration, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.id,
                    saved.id,
                    operation_id,
                    saved.revision,
                    player_input,
                    narration,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO session_changes (
                    id, session_id, revision, operation_id,
                    kind, changes_json, created_at
                ) VALUES (?, ?, ?, ?, 'snapshot', ?, ?)
                """,
                (
                    str(uuid4()),
                    saved.id,
                    saved.revision,
                    operation_id,
                    encoded,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO session_checkpoints (
                    id, session_id, revision, base_version_id,
                    reason, world_json, created_at
                ) VALUES (?, ?, ?, ?, 'turn', ?, ?)
                """,
                (
                    str(uuid4()),
                    saved.id,
                    saved.revision,
                    saved.base_version_id,
                    encoded,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO pending_notifications (
                    event_id, operation_id, session_id,
                    kind, identifiers_json, created_at
                ) VALUES (?, ?, ?, 'turn_committed', ?, ?)
                """,
                (
                    notice.event_id,
                    operation_id,
                    saved.id,
                    json.dumps(asdict(notice)),
                    now,
                ),
            )
            db.execute(
                """
                UPDATE turn_attempts
                SET state = 'completed'
                WHERE operation_id = ?
                """,
                (operation_id,),
            )
            db.execute(
                """
                UPDATE operation_receipts
                SET status = 'completed',
                    result_json = ?,
                    public_error_code = NULL
                WHERE operation_id = ?
                """,
                (result_json, operation_id),
            )
            db.commit()

        return result

    def accepted_history(self, owner_id: str, session_id: str) -> tuple[AcceptedTurn, ...]:
        self.load_session(owner_id, session_id)

        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT id, session_id, operation_id, revision,
                       player_input, public_narration
                FROM accepted_turns
                WHERE session_id = ?
                ORDER BY revision
                """,
                (session_id,),
            ).fetchall()

        return tuple(
            AcceptedTurn(
                id=row["id"],
                session_id=row["session_id"],
                operation_id=row["operation_id"],
                revision=row["revision"],
                player_input=row["player_input"],
                narration=row["public_narration"],
            )
            for row in rows
        )

    def adopt_update(
        self, owner_id: str, operation_id: str, expected_revision: int, candidate: SessionSnapshot
    ) -> SessionSnapshot:
        raise FeatureNotImplemented("PERSISTENCE")

    def record_attempt_failure(
        self,
        owner_id: str,
        operation_id: str,
        session_id: str,
        public_code: str,
        uncertain_provider_outcome: bool,
    ) -> None:
        safe_code = (
            public_code
            if public_code in {code.value for code in ErrorCode}
            else ErrorCode.INTERNAL.value
        )
        if uncertain_provider_outcome:
            safe_code = ErrorCode.PROVIDER_UNCERTAIN.value

        status = (
            "rejected"
            if safe_code
            in {
                ErrorCode.INVALID,
                ErrorCode.CONFLICT,
                ErrorCode.FORBIDDEN,
                ErrorCode.UNAUTHENTICATED,
            }
            else "failed"
        )

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            row = db.execute(
                """
                SELECT r.status
                FROM operation_receipts AS r
                JOIN turn_attempts AS t ON t.operation_id = r.operation_id
                WHERE r.operation_id = ?
                  AND r.owner_id = ?
                  AND t.session_id = ?
                """,
                (operation_id, owner_id, session_id),
            ).fetchone()
            if row is None or row["status"] != "running":
                return

            db.execute(
                """
                UPDATE turn_attempts
                SET state = ?, provider_outcome_uncertain = ?
                WHERE operation_id = ? AND state = 'running'
                """,
                (status, int(uncertain_provider_outcome), operation_id),
            )
            db.execute(
                """
                UPDATE operation_receipts
                SET status = ?, public_error_code = ?
                WHERE operation_id = ?
                """,
                (status, safe_code, operation_id),
            )
            db.commit()

    def pending_notifications(self) -> tuple[CommittedTurnNotice, ...]:
        raise FeatureNotImplemented("PERSISTENCE")

    def acknowledge_notification(self, consumer: str, event_id: str) -> None:
        raise FeatureNotImplemented("PERSISTENCE")

    def list_profiles(self, owner_id: str) -> tuple[ProviderProfile, ...]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT id, owner_id, name, endpoint, model, protocol,
                       configuration_revision,
                       credential_reference IS NOT NULL AS has_key
                FROM provider_profiles
                WHERE owner_id = ?
                ORDER BY name COLLATE NOCASE, id
                """,
                (owner_id,),
            ).fetchall()

        return tuple(
            ProviderProfile(
                id=row["id"],
                owner_id=row["owner_id"],
                name=row["name"],
                endpoint=row["endpoint"],
                model=row["model"],
                protocol=row["protocol"],
                configuration_revision=row["configuration_revision"],
                credential_present=bool(row["has_key"]),
            )
            for row in rows
        )

    def active_profile(self, owner_id: str) -> ProviderProfile:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT * FROM provider_profiles
                WHERE owner_id = ? AND is_active = 1
                """,
                (owner_id,),
            ).fetchone()

        if row is None:
            raise ApplicationError(ErrorCode.INVALID, "Choose an active provider card")

        return ProviderProfile(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            endpoint=row["endpoint"],
            model=row["model"],
            protocol=row["protocol"],
            configuration_revision=row["configuration_revision"],
            credential_present=row["credential_reference"] is not None,
        )

    def credential_reference(self, owner_id: str, profile_id: str, revision: int) -> str:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT configuration_revision, credential_reference
                FROM provider_profiles
                WHERE id = ? AND owner_id = ?
                """,
                (profile_id, owner_id),
            ).fetchone()

        if row is None:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card not found or access denied")
        if row["configuration_revision"] != revision:
            raise ApplicationError(
                ErrorCode.CONFLICT, "Provider card changed; reload before using it"
            )
        if row["credential_reference"] is None:
            raise ApplicationError(ErrorCode.INVALID, "Provider card has no credential")

        return row["credential_reference"]

    def apply_committed_notice(self, notice: CommittedTurnNotice) -> None:
        raise FeatureNotImplemented("PERSISTENCE")

    def campaign_report(
        self, owner_id: str, campaign_id: str, version_id: str | None
    ) -> CampaignReport:
        raise FeatureNotImplemented("PERSISTENCE")

    def save_editor_proposal(self, owner_id: str, proposal: EditorProposal) -> None:
        raise FeatureNotImplemented("PERSISTENCE")

    def load_editor_proposal(self, owner_id: str, proposal_id: str) -> EditorProposal:
        raise FeatureNotImplemented("PERSISTENCE")

    def resolve_editor_proposal(
        self, owner_id: str, proposal_id: str, status: str
    ) -> EditorProposal:
        raise FeatureNotImplemented("PERSISTENCE")

    def drafts_for_owner(self, owner_id: str) -> tuple[CampaignSummary, ...]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT c.id,
                    json_extract(c.draft_json, '$.title') AS title,
                    c.draft_revision,
                    (
                        SELECT p.id
                        FROM published_versions AS p
                        WHERE p.campaign_id = c.id
                        ORDER BY p.version DESC
                        LIMIT 1
                    ) AS published_version_id
                FROM campaigns AS c
                WHERE c.owner_id = ?
                ORDER BY c.updated_at DESC, c.id
                """,
                (owner_id,),
            ).fetchall()

        return tuple(
            CampaignSummary(
                row["id"],
                row["title"],
                row["draft_revision"],
                row["published_version_id"],
            )
            for row in rows
        )

    def sessions_for_owner(self, owner_id: str) -> tuple[SessionSummary, ...]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT s.id, s.base_version_id, s.revision,
                       json_extract(p.content_json, '$.title') AS campaign_title,
                       EXISTS (
                           SELECT 1 FROM published_versions AS newer
                           WHERE newer.campaign_id = s.campaign_id
                             AND newer.version > p.version
                       ) AS update_available
                FROM playthroughs AS s
                JOIN published_versions AS p ON p.id = s.base_version_id
                WHERE s.owner_id = ?
                ORDER BY s.updated_at DESC, s.id
                """,
                (owner_id,),
            ).fetchall()

        return tuple(
            SessionSummary(
                id=row["id"],
                campaign_title=row["campaign_title"],
                base_version_id=row["base_version_id"],
                revision=row["revision"],
                update_available=bool(row["update_available"]),
            )
            for row in rows
        )

    def save_profile(
        self,
        owner_id: str,
        profile: ProviderProfile,
        expected_revision: int | None,
        credential_reference: str | None,
    ) -> ProviderProfile:
        if profile.owner_id != owner_id:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card owner mismatch")
        if credential_reference is not None and not credential_reference.strip():
            raise ApplicationError(ErrorCode.INVALID, "Invalid credential reference")

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            existing = db.execute(
                """
                SELECT owner_id, configuration_revision
                FROM provider_profiles
                WHERE id = ?
                """,
                (profile.id,),
            ).fetchone()

            if existing is not None and existing["owner_id"] != owner_id:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card access denied")

            if expected_revision is None:
                if existing is not None:
                    raise ApplicationError(ErrorCode.CONFLICT, "Provider card already exists")
                revision = 1
                db.execute(
                    """
                    INSERT INTO provider_profiles (
                        id, owner_id, name, endpoint, model, protocol,
                        configuration_revision, credential_reference, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        profile.id,
                        owner_id,
                        profile.name,
                        profile.endpoint,
                        profile.model,
                        profile.protocol,
                        revision,
                        credential_reference,
                    ),
                )
            else:
                if existing is None:
                    raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card not found")
                if existing["configuration_revision"] != expected_revision:
                    raise ApplicationError(
                        ErrorCode.CONFLICT, "Provider card changed; reload before saving"
                    )
                revision = expected_revision + 1
                db.execute(
                    """
                    UPDATE provider_profiles
                    SET name = ?, endpoint = ?, model = ?, protocol = ?,
                        configuration_revision = ?, credential_reference = ?,
                        is_active = CASE WHEN ? IS NULL THEN 0 ELSE is_active END
                    WHERE id = ? AND owner_id = ?
                    """,
                    (
                        profile.name,
                        profile.endpoint,
                        profile.model,
                        profile.protocol,
                        revision,
                        credential_reference,
                        credential_reference,
                        profile.id,
                        owner_id,
                    ),
                )
            db.commit()

        return replace(
            profile,
            configuration_revision=revision,
            credential_present=credential_reference is not None,
        )

    def activate_profile(
        self, owner_id: str, profile_id: str, expected_revision: int
    ) -> ProviderProfile:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            row = db.execute(
                """
                SELECT * FROM provider_profiles
                WHERE id = ? AND owner_id = ?
                """,
                (profile_id, owner_id),
            ).fetchone()

            if row is None:
                raise ApplicationError(
                    ErrorCode.FORBIDDEN, "Provider card not found or access denied"
                )
            if row["configuration_revision"] != expected_revision:
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Provider card changed; reload before activating"
                )
            if row["credential_reference"] is None:
                raise ApplicationError(ErrorCode.INVALID, "Provider card has no credential")
            db.execute(
                "UPDATE provider_profiles SET is_active = 0 WHERE owner_id = ?",
                (owner_id,),
            )
            db.execute(
                """
                UPDATE provider_profiles SET is_active = 1
                WHERE id = ? AND owner_id = ?
                """,
                (profile_id, owner_id),
            )
            db.commit()

        return ProviderProfile(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            endpoint=row["endpoint"],
            model=row["model"],
            protocol=row["protocol"],
            configuration_revision=row["configuration_revision"],
            credential_present=True,
        )

    def delete_profile(
        self,
        owner_id: str,
        profile_id: str,
        expected_revision: int,
        replacement_profile_id: str | None,
    ) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            row = db.execute(
                """
                SELECT * FROM provider_profiles
                WHERE id = ? AND owner_id = ?
                """,
                (profile_id, owner_id),
            ).fetchone()

            if row is None:
                raise ApplicationError(
                    ErrorCode.FORBIDDEN, "Provider card not found or access denied"
                )
            if row["configuration_revision"] != expected_revision:
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Provider card changed; reload before deleting"
                )

            if replacement_profile_id is not None:
                if not row["is_active"] or replacement_profile_id == profile_id:
                    raise ApplicationError(
                        ErrorCode.INVALID, "Choose a different replacement for the active card"
                    )
                replacement = db.execute(
                    """
                    SELECT credential_reference FROM provider_profiles
                    WHERE id = ? AND owner_id = ?
                    """,
                    (replacement_profile_id, owner_id),
                ).fetchone()

                if replacement is None:
                    raise ApplicationError(
                        ErrorCode.FORBIDDEN, "Replacement card not found or access denied"
                    )
                if replacement["credential_reference"] is None:
                    raise ApplicationError(ErrorCode.INVALID, "Replacement card has no credential")

            db.execute(
                "DELETE FROM provider_profiles WHERE id = ? AND owner_id = ?",
                (profile_id, owner_id),
            )
            if replacement_profile_id is not None:
                db.execute(
                    """
                    UPDATE provider_profiles SET is_active = 1
                    WHERE id = ? AND owner_id = ?
                    """,
                    (replacement_profile_id, owner_id),
                )
            db.commit()

    def initialize(self) -> None:
        """Create schema v2 or add the editor columns to an existing v1 database."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                return
            if version not in (0, 1):
                raise ApplicationError(ErrorCode.STORAGE, "Unsupported database schema version")
            if version == 0:
                schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                db.executescript(f"""
                    BEGIN IMMEDIATE;
                    {schema}
                    INSERT INTO schema_versions (version, applied_at)
                    VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
                    PRAGMA user_version = 2;
                    COMMIT;
                """)
                return

            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(editor_proposals)")}
            additions = {
                "summary": "TEXT NOT NULL DEFAULT ''",
                "changes_json": "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(changes_json))",
                "resolved_at": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE editor_proposals ADD COLUMN {name} {definition}")
            db.execute(
                "INSERT INTO schema_versions (version, applied_at) "
                "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            db.execute("PRAGMA user_version = 2")
            db.commit()

    def admit_turn(
        self,
        owner_id: str,
        operation_id: str,
        request_id: str,
        session_id: str,
        expected_revision: int,
        player_input: str,
    ) -> TurnAdmission:
        payload = json.dumps(
            [session_id, expected_revision, player_input],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        fingerprint = sha256(payload.encode("utf-8")).hexdigest()

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE;")
            row = db.execute(
                """
                SELECT state_json FROM playthroughs
                WHERE id = ? AND owner_id = ?
                """,
                (session_id, owner_id),
            ).fetchone()
            if row is None:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Session not found or access denied")
            state = decode(load_json(row["state_json"]), SessionSnapshot)

            existing = db.execute(
                """
                SELECT request_digest, kind, status, result_json
                FROM operation_receipts
                WHERE owner_id = ? AND request_id = ?
                """,
                (owner_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["kind"] != "turn" or existing["request_digest"] != fingerprint:
                    raise ApplicationError(
                        ErrorCode.CONFLICT, "Request ID reused with different data"
                    )
                if existing["status"] == "completed":
                    return TurnAdmission(
                        baseline=None,
                        previous_commit=decode(load_json(existing["result_json"]), TurnCommit),
                    )
                raise ApplicationError(
                    ErrorCode.CONFLICT,
                    "This request already exists; check its status before retrying",
                )

            if state.revision != expected_revision:
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Session changed; reload before submitting"
                )
            if db.execute(
                "SELECT 1 FROM operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone():
                raise ApplicationError(ErrorCode.CONFLICT, "Operation ID already used")
            if db.execute(
                """
                SELECT 1 FROM turn_attempts
                WHERE session_id = ? AND state = 'running'
                """,
                (session_id,),
            ).fetchone():
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Another turn is in progress for this session"
                )

            db.execute(
                """
                INSERT INTO operation_receipts (
                    operation_id, owner_id, request_id, request_digest,
                    kind, status, created_at
                )
                VALUES (?, ?, ?, ?, 'turn', 'running',
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (operation_id, owner_id, request_id, fingerprint),
            )
            db.execute(
                """
                INSERT INTO turn_attempts (
                    id, operation_id, session_id, expected_revision,
                    state, player_input, provider_outcome_uncertain, created_at
                )
                VALUES (?, ?, ?, ?, 'running', ?, 0,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    str(uuid4()),
                    operation_id,
                    session_id,
                    expected_revision,
                    player_input,
                ),
            )
            db.commit()

        return TurnAdmission(baseline=state, previous_commit=None)
