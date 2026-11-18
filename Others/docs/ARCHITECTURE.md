# Two architectures, shared capabilities

Shared modules provide business capabilities; each architecture owns its workflow coordination.
UI calls ApplicationGateway, services use repository/provider ports, and composition roots supply
the adapters. Models and the codec have no I/O. Screens access infrastructure through services.

## Design baseline and refinement

Component boundaries, connector policies and diagrams can evolve under the change workflow in
[CONTRIBUTING](../CONTRIBUTING.md). Keep the two implementations functionally equivalent.
These views cover application responsibilities and their provider/storage boundaries.

## Repository and build boundaries

The repository contains two applications, not one app with an architecture switch:

- `layered/`: Layered entrypoint, composition root, coordination and build target.
- `event_based/`: Event-Based entrypoint, composition root, coordination and build target.
- `shared/`: models, capabilities, adapters and UI components used by both.

Each app supplies one gateway factory to the shared UI shell. Dependencies point from each app to
`shared`, never between the apps or from `shared` back to an app. Build targets use common packaging
utilities but read only their own application's code and common inputs. The other application can be
absent during a build. Bundles retain the same entrypoints used during development, with separate
server configuration and local data paths. See [build instructions](../README.md#build-each-demo).

## Layered

`ScreenController → LayeredGateway → LayeredApplication → domain-facing services → infrastructure ports`.
This is the command path. Queries go from LayeredGateway directly to QueryService; polling reads
OperationStatusStore. Results return to the caller. A turn calls prepare, capture, privileged
planning with accepted history, validation, disclosure authorization, player projection, narration, atomic commit, then reporting.
A report failure after commit returns the accepted turn with reporting-pending status.
Reporting catch-up replays durable committed notices without repeating model requests.

## Event-Based

`ScreenController → EventGateway → Queue → registered component handler`.
Publishing returns without executing a handler. A separate worker dispatches subscriptions. Mutating
commands have distinct topics. Read-only queries may use authorized projections directly.

Implemented normal-turn topology:

1. `turn.requested`: authenticate, admit/deduplicate, capture state/configuration and private work reference.
2. `turn.context_prepared`: Director model handler creates typed plan.
3. `turn.plan_proposed`: validator computes candidate and permitted player projection.
4. `turn.validated_for_narration`: narrator handler retrieves private NarrationWork, then passes only its PlayerView to AIService.narrate.
5. `turn.narration_proposed`: persistence handler atomically commits state/transcript/receipt/notice.
6. `turn.committed`: **independent result and reporting subscribers**; each acknowledges separately.
7. `turn.rejected_or_failed`: attempt history and safe status, no accepted-state mutation.

The editor sequence is EDITOR_REQUEST → EDITOR_CONTEXT → EDITOR_CANDIDATE:
prepare/capture → model proposal → persist for explicit review.
Account, campaign, profile and session commands also use queued handlers. Multistage handlers
publish continuation messages; terminal handlers complete or acknowledge the operation. The bus handles routing.

The current queue is unbounded and in-memory. One worker invokes subscribers sequentially, with separate
error handling for each subscriber. Shutdown drops queued commands and fails their transient receipts;
running network calls use durable attempt state and explicit retry. Startup recovery republishes
committed notification identifiers, never paid provider requests. Before serving requests, both
application bootstraps also mark abandoned running attempts failed with an uncertain provider outcome.
That startup cleanup preserves saved gameplay and assumes one host per database.

## Component → class/module → connector map

| Component | Implementing class/module | Connector |
|---|---|---|
| Shared UI | ScreenController, shared/ui page functions | ApplicationGateway.submit/poll/query |
| Layered application | LayeredGateway, LayeredApplication | Direct typed service calls |
| Event ingress | EventGateway | InProcessBus.publish; status/query interface |
| Event connector | InProcessBus | Queue + topic/subscription callback |
| Event components | ComponentHandlers named methods | Message topics and opaque work references |
| Private stage state | WorkflowStateStore, EditorWork, TurnWork, NarrationWork | Owner-scoped opaque references; private NarrationWork retains candidate state, while the narrator provider receives only PlayerView |
| Event recovery | CompletionRecovery | SessionRepository pending notices; bus publication |
| Accounts | AccountService, ScryptPasswordHasher | AccountRepository / PasswordHasher |
| World/publication | WorldService | CampaignRepository |
| Coeditor | EditorService | EditorProposalRepository + AIService |
| Session/gameplay | GameplayService | CampaignRepository / SessionRepository |
| Disclosure/context | KnowledgePolicy | Typed internal state → authorized DTO |
| AI generation | AIService | ProviderTransport |
| API cards | ProfileService | ProfileRepository / CredentialStore |
| Updates | UpdateService | CampaignRepository / SessionRepository |
| Reporting | ReportingService | ReportRepository; direct call or independent subscriber |
| Queries | QueryService | Authorized projections through domain/repository ports |
| Shared persistence | SQLiteStore, schema.sql | Typed repository ports / short SQL transactions |
| Secrets | LocalCredentialStore | OS keyring port |
| HTTP / deterministic test | CompatibleHttpProvider / ScriptedTestProvider | ProviderTransport.complete |
| Transient status | OperationStatusStore | Process-local receipts |

Diagrams: [Layered](diagrams/layered.mmd), [Event-Based](diagrams/event-based.mmd),
[class/port overview](diagrams/classes.mmd). These are selected overview views, not exhaustive
class or call graphs. Event workflow nodes represent ComponentHandlers methods, not separate
processes. Dashed event-diagram arrows denote queued publication/dispatch; solid arrows denote
local calls or data access. [STATUS](STATUS.md) records implementation and verification status.

## Connector characterization

Describe each boundary using the course's connector types and dimensions. Identify relevant requires,
prohibits, restricts and cautions relationships between their properties.

| Boundary | Baseline and status | Properties to specify/evaluate |
|---|---|---|
| Gateway/service calls | Typed, local, synchronous procedure calls | Entry points, parameters, accessibility, cardinality, error propagation |
| Event publication/subscription | Queued dispatch implemented; one FIFO worker, sequential subscribers, no durable queue | Delivery, ordering/causality, notification, capacity, scheduling, failure isolation |
| Repository/SQLite | SQLite adapter with short atomic transactions, notices, reports and proposals | Data access, transaction scope, locking, availability, concurrent updates |
| Provider adapter/HTTP | OpenAI-compatible, Anthropic and Gemini nonstreaming adapters | Adaptation, deadlines, response bounds, credential scope, uncertain remote outcomes |

## Risk analysis and comparison evidence

Existing experiments and recorded results are in [the risk report](risk/REPORT.md).

For each selected risk, record its cause, affected requirement, test scenario, observed consequence,
mitigation and remaining limitation. Evaluate both architectures using comparable conditions.

| Risk | Recorded experiment | Status and evidence |
|---|---|---|
| Single-worker blocking | E1: controlled slow handler and independent burst | Complete: queue wait, throughput and pending depth |
| Queued work at shutdown | E2: close with pending synthetic messages | Complete: dispatch, discard and restoration accounting |
| Layered provider latency | L1: two controlled sequential AI delays | Complete: per-stage and total response timing |
| Reporting delay/failure | L2: post-commit reporting delay and exception | Complete: response delay, preserved result and pending flag |
| Private data crossing an interface | P1: tagged private and credential canaries through both gateways | Complete: exact provider-boundary checks and negative public/trace assertions |
| Commit-before-notification interruption | R1: interrupt after commit and before acknowledgements in both gateways | Complete: preserved state, recovery time, two consumer acknowledgements and zero provider replay |
| Admitted-turn interruption | R2: restart after durable admission but before commit in both gateways | Complete: failed/uncertain classification, no automatic provider replay, linked explicit retry and version attribution |

Use the same campaign, input sequence, prompts, scripted responses/delays and persistence semantics.
Record the environment, versions, traces, sample counts and ranges. Measure provider time separately
from submit-to-confirm, dispatch, persistence, report lag and recovery. The seven completed experiments
cover the selected risks above. Stale updates, partial reporting writes, concurrent load, duplicate
delivery and additional storage/subscriber failure points remain follow-up scenarios rather than
recorded experiment evidence. Use the completed workflow evidence without claiming a universal style
winner.
