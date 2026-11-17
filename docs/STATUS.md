# Implementation status

## MVP implementation checkpoint — November 15, 2026

The proposal's author-and-play MVP is implemented through both architecture gateways. The two
applications share domain rules, DTOs, SQLite repositories, provider adapters, credential storage,
and Streamlit pages; they differ in workflow coordination and connectors.

| Area | Current state |
|---|---|
| Accounts | Register, salted password hashing, login/logout, and owner-scoped access |
| Campaign authoring | Friendly typed forms, advanced typed JSON, readiness checks, immutable publication |
| Creative writing | Scoped AI proposals, stored diffs, explicit accept/reject, separate Save and Publish |
| Sessions/gameplay | Preset-PC sessions, isolated state, validated effects, disclosure gates, save/resume |
| Layered | Direct ordered call/return turn, retry, update, reporting, and query workflows |
| Event-Based | Queued staged editor and gameplay workflows with opaque private state and independent subscribers |
| Persistence | SQLite drafts, versions, sessions, attempts, commits, checkpoints, notices, reports, and proposals |
| Reporting | Attempt counts, false-to-true milestone hits, denominators, freshness, idempotent catch-up |
| Retry/recovery | Explicit linked retry; committed-notice replay after restart without provider replay |
| Campaign updates | Conservative field-aware three-way preview and atomic opt-in adoption with checkpoint |
| Providers | OpenAI-compatible (including hosted Meta models), Anthropic Messages, and Google Gemini adapters |
| API cards | Per-account cards; keys stored in the OS credential vault and never in SQLite |
| UI | One shared full-coverage UI used by two separately launched applications |

## Verification

- `tests/test_event_gameplay_mvp.py` exercises the full queued turn, commit, result/report
  subscribers, milestone projection, and restart recovery without another model call.
- `tests/test_layered_turn.py::LayeredTurnTests.test_turn_commits_and_replays_after_reopening`
  exercises Layered restart catch-up without another model call; the same module also covers commit
  replay, invalid effects, uncertain outcomes, and linked retry.
- Dedicated risk tests cover private-canary isolation through both gateways and controlled
  commit-before-acknowledgement interruption/restart recovery in both architectures.
- Provider tests verify all three supported request/response protocols against a local HTTP server.
- Streamlit AppTest renders both entrypoints and exercises authentication and campaign creation.
- Final verification: 98 tests pass with one Windows symlink-privilege skip; Ruff, formatting,
  compilation, structural, public-tree, wiring, isolated-build, and Streamlit checks pass.
- No live external-provider request is made by the automated suite.

The A3 real-credential portion remains a manual environment check because automated tests must not
contain or spend a user's provider credentials. Marketplace/payments, multiplayer, custom-PC
creation, arbitrary rulebook interpretation, and automatic conflict resolution remain outside MVP.
