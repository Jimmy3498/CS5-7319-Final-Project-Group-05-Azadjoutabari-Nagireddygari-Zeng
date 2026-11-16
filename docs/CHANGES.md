# Integration changes — November 14, 2026

> November 15 follow-up: completed Event-Based gameplay, reporting/milestones, linked retry,
> committed-notice recovery, compatible update adoption, friendly authoring forms, and native
> OpenAI-compatible/Anthropic/Gemini adapters. Added the full queued gameplay/recovery scenario in
> `tests/test_event_gameplay_mvp.py`. See [STATUS](STATUS.md) for the current checkpoint.

## Baseline and counts

This checkpoint combines local backend work, Farhad's creative-writing work from main
`ff9dff3eab4ddf35ecc78fa71f1c4bfb59f4d40c`, and the risk branch at
`3ff67a8319907bf1dd502ce3773e9574a1354a7b`.

Counts compare the combined source with the original shared scaffold
`1bd8790ae29a01007c55b41dc3ef76c396b065df`. Each file appears in one category.

| Category | Modified | Added | Total changed |
|---|---:|---:|---:|
| Existing implementation files filled or extended | 26 | 0 | 26 |
| New implementation module | 0 | 1 | 1 |
| Tests and check/build/experiment tooling | 6 | 20 | 26 |
| Documentation, configuration and risk evidence | 8 | 12 | 20 |
| **Total** | **40** | **33** | **73** |

The public source contains **121 files**. Compared with the pinned current main, this checkpoint
adds **32 files** and changes **32 files**, with no deletions. Counts include formatting cleanup;
a changed file does not necessarily represent a new feature.

## 1. Existing implementation filled or extended

- Accounts and authentication, draft persistence, validation, publication and catalog.
- Session creation/list/resume, isolated snapshots and player-visible projections.
- Ordered typed effects, disclosure gates, durable admission, atomic turn commit and failure receipts.
- API-card management, credential adapter, planning/narration and compatible HTTP transport.
- Layered gameplay orchestration and separate startup initialization.
- Farhad's reviewed-editor service and Layered/Event-Based editor coordination.

## 2. Necessary additions

The only new runtime module is `shared/domain/conditions.py`: one shared evaluator for
authored location, inventory and flag conditions.

Several additions extend existing files rather than create new modules:
- Session setup and director-context fields carry the opening, private context and accepted history.
- EditorCompletion, EditorChange and proposal status/diff fields support reviewed editing.
- EditorWork and private state retention support queued editor stages.
- The proposal-resolution port, schema columns and SQLite methods support editor persistence.
- A small v1-to-v2 initialization upgrade adds the editor columns while preserving existing data.
  This is a single schema upgrade, not a general migration framework.

## 3. Tests and helpers

There are **18 changed/new test modules** and **8 changed/new tooling files** in this checkpoint.
Keep these in the repository. Tests and experiment scripts are excluded from standalone demo
bundles; the packaging helper is needed to build those bundles from source.

The tests cover the foundation, privacy/effects, turn transactions, profiles, HTTP transport,
Layered gameplay, creative writing, schema preservation and the four risk scenarios.
Broader acceptance placeholders remain skipped.

Risk measurements and their original environment/provenance are retained unchanged.
Only the Layered harness's director-context signature and expected history-call order were
updated for compatibility; the stored benchmark data was not regenerated.

## Merge decisions and cleanup

- Preserve local Layered gameplay; add the selected-record argument for editor proposal retention.
- Combine local AI planning/narration with Farhad's coeditor method.
- Preserve local WorldService validation/readiness/publication.
- Combine local SQLite operations with the new editor proposal contract and schema.
- Retain the expanded DirectorContext and add EditorCompletion.
- Keep Farhad's editor-state cleanup and continuation-event flow.
- Fix old structural assertions that expected implemented routes to be unimplemented.
- Remove unused/duplicate imports and apply the existing Ruff formatting rules.
- Make structural/wiring helper scripts use temporary databases.
- Update status text, run instructions and diagram labels; keep runtime secrets/data ignored.

The architecture remains one repository with separate Layered and Event-Based builds.
No marketplace, multiplayer, custom-PC creation, rulebook engine or new deployment framework
was added. Remaining features are listed in [STATUS](STATUS.md).

## Verification

- **115 tests run: 87 passed, 28 skipped, zero failures/errors.**
- Ruff check and format check passed.
- Structural routing/schema checks and direct-versus-queued wiring smoke check passed.
- Source/link checks passed.
- Tests include localhost HTTP, Streamlit preview and independently extracted demo builds.

Environment: macOS arm64, Python 3.14.3, Streamlit 1.66.1, Ruff 0.17.3.
The declared minimum is Python 3.12; a new Python-3.12 dependency installation was not tested here.
No external provider or real credential-vault calls were made. The skipped tests are unfinished
end-to-end acceptance cases, not evidence of completed feature parity.

## Changed-file inventory

M = modified from the initial scaffold; A = added. Paths are repository-relative.

### Existing implementation

- **M** `event_based/bootstrap.py`
- **M** `event_based/gateway.py`
- **M** `event_based/handlers.py`
- **M** `event_based/state.py`
- **M** `layered/application.py`
- **M** `layered/bootstrap.py`
- **M** `shared/codec.py`
- **M** `shared/domain/accounts.py`
- **M** `shared/domain/ai.py`
- **M** `shared/domain/editor.py`
- **M** `shared/domain/gameplay.py`
- **M** `shared/domain/knowledge.py`
- **M** `shared/domain/profiles.py`
- **M** `shared/domain/queries.py`
- **M** `shared/domain/world.py`
- **M** `shared/infrastructure/provider.py`
- **M** `shared/infrastructure/schema.sql`
- **M** `shared/infrastructure/secrets.py`
- **M** `shared/infrastructure/security.py`
- **M** `shared/infrastructure/sqlite.py`
- **M** `shared/models/ai.py`
- **M** `shared/models/gameplay.py`
- **M** `shared/models/world.py`
- **M** `shared/ports.py`
- **M** `shared/ui/app.py`
- **M** `shared/wiring.py`

### New implementation module

- **A** `shared/domain/conditions.py`

### Tests and tooling

- **A** `.github/workflows/structural-checks.yml`
- **M** `scripts/check_scaffold.py`
- **M** `scripts/demo_bundle.py`
- **A** `scripts/risk/event_backlog.py`
- **A** `scripts/risk/event_shutdown.py`
- **A** `scripts/risk/layered_latency.py`
- **A** `scripts/risk/layered_reporting.py`
- **M** `scripts/wiring_smoke.py`
- **A** `tests/test_ai_service.py`
- **A** `tests/test_architecture_foundation.py`
- **A** `tests/test_creative_writing.py`
- **M** `tests/test_demo_builds.py`
- **A** `tests/test_effect_validation.py`
- **A** `tests/test_foundation.py`
- **A** `tests/test_http_provider.py`
- **A** `tests/test_layered_turn.py`
- **A** `tests/test_profile_service.py`
- **A** `tests/test_profile_storage.py`
- **A** `tests/test_risk_event_backlog.py`
- **A** `tests/test_risk_event_shutdown.py`
- **A** `tests/test_risk_layered_latency.py`
- **A** `tests/test_risk_layered_reporting.py`
- **A** `tests/test_schema_upgrade.py`
- **M** `tests/test_structure.py`
- **A** `tests/test_turn_commit.py`
- **M** `tests/test_ui.py`

### Documentation, configuration and risk evidence

- **A** `.env.example`
- **M** `.gitignore`
- **A** `.streamlit/config.toml`
- **M** `CONTRIBUTING.md`
- **M** `README.md`
- **M** `docs/ARCHITECTURE.md`
- **A** `docs/CHANGES.md`
- **M** `docs/STATUS.md`
- **M** `docs/diagrams/classes.mmd`
- **M** `docs/diagrams/event-based.mmd`
- **A** `docs/risk/REPORT.md`
- **A** `docs/risk/results/E1_raw.csv`
- **A** `docs/risk/results/E1_summary.json`
- **A** `docs/risk/results/E2_raw.csv`
- **A** `docs/risk/results/E2_summary.json`
- **A** `docs/risk/results/L1_raw.csv`
- **A** `docs/risk/results/L1_summary.json`
- **A** `docs/risk/results/L2_raw.csv`
- **A** `docs/risk/results/L2_summary.json`
- **M** `pyproject.toml`
