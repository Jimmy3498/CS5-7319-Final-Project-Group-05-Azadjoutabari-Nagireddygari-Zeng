# Contributing

Choose work by capability so each implementation supports both architectures. Assign owners as a team.

| Work ID | Deliverable | Dependencies |
|---|---|---|
| AUTH | Accounts, password hashes, login/logout and ownership | SQLite + UI |
| WORLD | World editing, drafts, readiness and publication | Models + SQLite |
| EDITOR | AI proposals, review, stale-buffer checks and Save | WORLD + PROVIDER + UI |
| PRIVACY | Role/session views and disclosure gates | WORLD + GAMEPLAY; required before live narration |
| GAMEPLAY | Sessions, supported effects, item creation and accepted turns | WORLD + PRIVACY + PERSISTENCE |
| MERGE | Compatible updates and retained checkpoints | WORLD + GAMEPLAY + PERSISTENCE |
| PROFILES / PROVIDER | Private API cards, credential storage and HTTP adapter | AUTH |
| REPORTS | Activity counts and milestone projections | Accepted events + attempt records |
| PERSISTENCE | Repositories, transactions, receipts and recovery | Shared dependency; coordinate schema edits |
| LAYERED | Application call sequence and error/retry handling | Shared capabilities |
| EVENTS | Queued handlers, private workflow state and recovery | Shared capabilities |
| UI | Bind screens to commands and queries | AUTH + each capability |

## Workflow

1. Read the function contract and acceptance case.
2. Implement the capability with synthetic test data.
3. Integrate it through both architectures; keep orchestration in its architecture directory.
4. Replace the skipped acceptance bodies with assertions and enable the cases for both styles.
5. Update affected contracts, diagrams and implementation status.

The account/campaign foundation and Layered turn path are implemented. Choose remaining work from
[STATUS](docs/STATUS.md), and coordinate edits to shared services and the SQLite schema.

## Architecture changes

Components, interfaces and connector policies can evolve. Keep feature behavior and data semantics
equivalent, and retain direct Layered coordination versus queued Event-Based coordination. Record the
reason for a change and update its consumers, tests and diagrams together.

For connector changes, describe invocation/delivery, ordering, cardinality, failure propagation and
recovery. Follow the comparison experiments in [ARCHITECTURE](docs/ARCHITECTURE.md).

## Repository practices

- Screens use application interfaces. SQL, provider and credential access belongs in adapters.
- Keep reusable code in `shared/` and coordination in `layered/` or `event_based/`. The applications
  never import each other; shared code imports neither. Build each target from the same repository.
- Services enforce account, session and knowledge restrictions.
- Keep credentials, private transcripts, databases, `.env`, `.local` and `.venv` out of Git.
- Run `scripts/check_public_tree.py` and review staged files before pushing.
- Retain contribution history and provenance; agree on a license.
- Default tests use scripted providers, fake vaults and temporary localhost HTTP servers.
  External provider checks are separate. Keep regression tests in the repository; demo builds exclude them.

## Course hand-in

Prepare both working implementations, Level-2 component/connector views, class mappings,
a system-specific comparison with graduate risk evidence, a **Word-format setup/architecture guide**,
slides, a presentation/demo video of at most **18 minutes**, and the public repository.

Complete implementations and submission are due **November 17, 2026, 11:59 p.m. Central**.
The **December 3** extension covers repository documentation only. Check Canvas for changes.
