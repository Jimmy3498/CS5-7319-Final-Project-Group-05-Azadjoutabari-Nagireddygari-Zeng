# Implementation status

## Local integration checkpoint — November 14, 2026

Combines the shared/backend work with Farhad's creative-writing contribution from
main `ff9dff3` and risk-analysis work from `risk_analysis` at `3ff67a8`.
See [change summary](CHANGES.md) for file counts and merge decisions.

| Area | Current state |
|---|---|
| Accounts | Registration, salted password hashing, login/logout and ownership checks work |
| Campaigns | Draft create/save/load, readiness validation, immutable publication and catalog work |
| Sessions | Preset-PC selection, isolated snapshots, owner-only list/resume and version warnings work |
| Gameplay | Typed effects, disclosure checks, private/public AI contexts, durable admission and atomic commit work |
| Layered | Complete turn path is tested with scripted responses; failures preserve accepted state; committed turns leave reporting pending |
| Event-Based | Account/campaign/profile/session handlers work; three-stage editor workflow implemented; multistage gameplay handlers remain TODO |
| Creative writing | Selected-record proposals, diffs and explicit review work with an in-memory proposal repository; SQLite proposal save/load/resolve and UI binding remain TODO |
| Providers | Private API-card services, OS-vault adapter and compatible HTTP transport implemented; tests use fake vaults and localhost HTTP |
| Persistence | Accounts, campaigns, sessions, turns and cards implemented; schema v2 upgrades v1 editor tables without resetting records |
| Reporting | Risk experiments available; application reports, milestones and completion-notice consumption remain TODO |
| Retry/recovery/updates | Explicit retry, interrupted-operation recovery and compatible campaign adoption remain TODO |
| UI | Seven preview pages; business controls disabled; empty-registration probe returns REJECTED |
| Packaging | Independent Layered and Event-Based bundles, each with shared code and its own database |

## Verification

The full suite includes service/persistence, separate-build and Streamlit-preview tests.
Broader A0–A13 acceptance tests remain skipped until their complete workflows exist.
Live external-model output and real OS-vault operation have not been tested in this checkpoint.
Recorded totals and check results are in [CHANGES](CHANGES.md#verification).

## Next implementation steps

1. Implement SQLite editor proposal save/load/resolve and test the editor with that adapter.
2. Complete Event-Based gameplay stages, private workflow state and failure cleanup.
3. Bind the UI to working services.
4. Complete reporting, retry/recovery and campaign-update workflows; enable their acceptance tests.
5. Rerun comparable completed workflows for the final architecture evaluation.

## Risk evidence

[Farhad's report](risk/REPORT.md) and CSV/JSON results retain the original measurements.
The Layered test harness now accepts history in director context and expects the updated call order.
Short regression tests do not replace a fresh benchmark run against the integrated application.

## Presentation and documentation handoff

Use `layered/`, `event_based/` and `shared/`; SQLite is the implemented database.
The UI calls gateways in-process. The Event-Based connector is a single-worker in-memory queue.
The HTTP adapter supports the chat-completions-compatible protocol.
Player identities come from campaign presets.

Actual class mappings and connector descriptions are in [ARCHITECTURE](ARCHITECTURE.md).
Use the [README](../README.md) for installation, launch and separate-build commands.
The presentation and Word drafts need these updates, working/unfinished labels and measured
risk evidence before submission.
