# Architectural Risk Experiment Report

This report collects the six architectural-risk experiments for the storytelling project. Each
experiment has the same reporting structure: a short description, procedure, observed results,
comparison, limitations and a concluding risk assessment. Results are reported only after an
experiment has been executed; pending sections do not contain estimated or invented measurements.

Passing an experiment's unit tests means that its harness collected valid data and preserved the
expected correctness properties. It does not necessarily mean that the architecture has acceptable
performance, recovery or privacy behavior. The risk conclusion comes from the measured behavior and
its consequences, not from whether the harness itself passed.

## Experiment status

| ID | Scenario | Status | Evidence |
|---|---|---|---|
| E1 | Single-worker queue bottleneck | Complete | [Raw CSV](results/E1_raw.csv), [summary JSON](results/E1_summary.json) |
| E2 | Pending-event loss during controlled shutdown | Complete | [Raw CSV](results/E2_raw.csv), [summary JSON](results/E2_summary.json) |
| L1 | Layered synchronous AI latency | Complete | [Raw CSV](results/L1_raw.csv), [summary JSON](results/L1_summary.json) |
| L2 | Layered reporting delay and failure | Complete | [Raw CSV](results/L2_raw.csv), [summary JSON](results/L2_summary.json) |
| P1 | Cross-architecture private-data isolation | Complete | [Raw CSV](results/P1_raw.csv), [summary JSON](results/P1_summary.json) |
| R1 | Cross-architecture commit-then-interrupt recovery | Complete | [Raw CSV](results/R1_raw.csv), [summary JSON](results/R1_summary.json) |

All six final campaigns were executed **serially**, one experiment at a time, in separate clean detached
worktrees at the same source commit. The generated timestamp in each summary is the end-of-run artifact
time, not a shared batch timestamp. This removes concurrent experiment load as a timing confound.
The final automated validation ran 98 tests successfully with one Windows symlink-privilege skip, and
Ruff reported no issues. Script digests in the summaries match the exact clean-worktree bytes executed;
Git's Windows line-ending checkout filter can produce a different raw-byte digest in another worktree
without changing the committed source text.

## E1 — Single-Worker Queue Bottleneck

### Short description

E1 evaluates how the existing Event-Based connector behaves when a burst of independent events is
handled by one worker. The production `InProcessBus` has an unbounded in-memory queue and one worker
that invokes subscribers sequentially. The experiment does not use the gameplay handlers. It
registers one synthetic handler for `turn.context_prepared` and introduces a controlled processing
delay so that the queueing behavior can be isolated from AI, database and domain behavior.

The risk hypothesis is:

> If one event handler is slow, later independent events will be blocked behind it. Queue waiting time
> will grow with event position, limiting throughput and increasing tail latency even when every event
> is eventually processed correctly.

This is a connector-level experiment, not an end-to-end gameplay throughput test.

### Test procedure

1. Instantiate a fresh production `InProcessBus` and register one synthetic handler for
   `Topic.TURN_CONTEXT`.
2. Start the real bus worker. No replacement bus, inline dispatcher or external service is used.
3. Create 20 valid intermediate `Message` objects with unique event, operation, request, client-session
   and correlation identities. Each message carries an opaque synthetic `work_reference` and no
   command.
4. Publish all 20 events as quickly as possible.
5. Record publication completion, handler start and handler finish with
   `time.perf_counter_ns()`. Per-event synchronization prevents the worker from racing ahead of the
   publication-completion timestamp.
6. Sample the bus's pending count during publication and processing. This is recorded as maximum
   **observed** pending depth because `Queue.qsize()` is an instantaneous concurrent observation and
   excludes work already executing in the handler.
7. Execute two conditions:
   - Baseline: no artificial handler delay.
   - Slow processing: the handler blocks for 0.5 seconds per event.
8. Repeat each condition three times with a fresh bus, giving 60 events per condition and 120 events
   overall.
9. Wait with a bounded timeout, record incomplete events explicitly and close the bus after each run.
10. Calculate queue wait, processing time, end-to-end latency, elapsed burst time, observed throughput,
    processed-event count and maximum observed pending depth.

The fast unit tests separately verify that events are dispatched, each event is counted once, all
required timestamps are present and output files have the declared fields. They do not rerun the
30-second experiment during every normal test-suite execution.

### Test configuration and provenance

| Property | Value |
|---|---|
| Experiment ID | E1 |
| Event topic | `turn.context_prepared` |
| Events per repetition | 20 |
| Repetitions per condition | 3 |
| Handler delays | 0.0 seconds and 0.5 seconds |
| Worker count | 1 |
| Clock | `time.perf_counter_ns()` |
| Python | CPython 3.12.4 |
| Platform | Windows 11 |
| Git commit | `173d2072af71d359579caa46210171add54794f0` |
| Working tree | Clean at experiment start (`working_tree_dirty=false`) |
| Execution | Serial; no other risk campaign running |
| Generated | November 17, 2026 at 01:20 UTC |

The experiment script's SHA-256 digest is retained in the summary JSON so that the results can be
matched to the exact measured script. The recorded commit contains the experiment script and tests,
and the clean-tree flag confirms that no uncommitted source changes were included in the run.

### Results and comparison

All 120 events completed, all 120 event IDs were unique, every handler was invoked exactly once, and
the bus reported no errors. Therefore, the observed delay is not caused by message loss, duplicate
handling or failed processing.

| Measurement | Baseline: 0.0 s delay | Slow: 0.5 s delay |
|---|---:|---:|
| Completed events | 60 / 60 | 60 / 60 |
| Incomplete events | 0 | 0 |
| Bus errors | 0 | 0 |
| Mean elapsed time per 20-event burst | 0.522 ms | 10,011.206 ms |
| Mean queue waiting time | 0.286 ms | 4,755.221 ms |
| P95 queue waiting time | 0.475 ms | 9,010.488 ms |
| Maximum queue waiting time | 0.491 ms | 9,511.218 ms |
| Mean handler processing time | 0.002 ms | 500.475 ms |
| P95 end-to-end event latency | 0.478 ms | 9,511.193 ms |
| Maximum end-to-end event latency | 0.494 ms | 10,011.820 ms |
| Mean observed throughput | 49,554.698 events/s | 1.998 events/s |
| Maximum observed pending depth | 20 | 20 |

The baseline handler performs essentially no work, so its throughput value is not a production
capacity estimate. Its purpose is to demonstrate that publication and dispatch add little latency when
the worker is not blocked. Maximum pending depth reached 20 in both conditions because the publication
burst was faster than worker scheduling. The important difference is how long events remained queued:
the baseline drained almost immediately, whereas the delayed condition required approximately ten
seconds.

With 0.5 seconds of work per event, one worker has a theoretical service capacity of approximately two
events per second. The observed 1.998 events per second closely matches that limit. Queue wait grew with
event position: the first event began almost immediately, while the last event waited approximately
9.5 seconds before its own 0.5-second processing interval. The p95 queue wait was approximately 18
times the processing requirement of one event:

```text
9.010 seconds p95 waiting / 0.5 seconds processing = approximately 18 times
```

The expected processing timeline is therefore serial:

```text
Worker: [E1 0.5s][E2 0.5s][E3 0.5s] ... [E20 0.5s]
Queue:       E2-E20       E3-E20              E20
```

Passing the E1 tests confirms that the experiment and connector behaved correctly; it does not remove
the latency exposure. E1 is a performance and scalability risk demonstration rather than a functional
correctness failure.

### Limitations

- The handler uses a controlled sleep and does not represent a complete gameplay turn.
- No AI provider, SQLite repository or network request is involved.
- Only one burst size and one nonzero service delay were measured.
- The result characterizes the current in-process connector on the recorded environment; it is not a
  production capacity forecast.
- The experiment does not compare the Event-Based result with the Layered architecture.
- Queue depth is sampled rather than continuously instrumented, so only maximum observed depth is
  reported.

These limitations restrict the scope of the conclusion but do not invalidate the demonstrated
single-worker serialization behavior. The production bus and worker policy under test are real.

### Scenario summary and why this is a risk

E1 demonstrates that the Event-Based connector preserves and processes a burst correctly, but a single
slow handler causes head-of-line blocking for every later event. Each event required only 0.5 seconds of
processing, yet the p95 event spent approximately 9.0 seconds waiting and the final end-to-end latency
reached approximately 10.0 seconds. This is evidence of latency amplification caused by the worker
policy, not merely evidence that an artificial delay lasted ten seconds.

This behavior should be considered an architectural risk because real handlers are expected to perform
provider, persistence and reporting work. If the arrival rate exceeds the approximately two-event-per-
second service rate demonstrated by a 0.5-second handler, backlog grows instead of clearing. Since the
current queue is unbounded and in memory, a sustained backlog can increase memory use, delay unrelated
operations and increase the number of queued events exposed to loss during process shutdown. Potential
mitigations include workload-specific queues, additional workers, nonblocking external calls, bounded
queues with backpressure and queue-latency monitoring. Those mitigations introduce ordering,
idempotency and session-concurrency concerns that must be evaluated separately.

## E2 — Pending-Event Loss During Controlled Shutdown

### Short description

E2 evaluates what the current `InProcessBus` does when the application closes while synthetic messages
remain in its in-memory queue. The experiment compares complete manual dispatch against a controlled
shutdown performed before any dispatch. It uses the production bus and its existing `close()` behavior,
without starting a background worker or modifying the queue.

The risk hypothesis is:

> Pending events exist only in process memory. If the bus closes before dispatch, `close()` removes the
> queued messages and reports shutdown failures, but a new bus cannot restore them because the queue is
> not durable and committed-notice recovery does not reconstruct pre-dispatch messages.

This is specifically a **controlled shutdown with queued synthetic messages**. It is not an operating-
system crash test and does not claim that a successfully processed or committed gameplay turn was
lost.

### Test procedure

1. Create ten valid `turn.context_prepared` messages with unique event, operation, request,
   client-session and correlation IDs. Each message contains an opaque synthetic `work_reference` and
   no command.
2. Register a synthetic handler on a real `InProcessBus`. The handler records successfully processed
   event IDs.
3. Register an error callback that records only event ID, operation ID, component and a controlled
   failure category. Exception text is deliberately excluded.
4. For the normal-completion scenario, publish all ten messages without starting a worker, invoke
   `dispatch_one()` exactly ten times, verify all handlers executed, and close the empty bus.
5. For the early-shutdown scenario, create a fresh bus, publish ten messages without dispatching them,
   confirm that all ten are pending, and call `close()`.
6. Record the shutdown callbacks, processed-event count, final queue depth and close duration.
7. Create another fresh bus and inspect its initial queue depth to determine whether any old event was
   restored.
8. Compare submitted, dispatched, discarded and restored event counts without asserting timing
   thresholds.

`CompletionRecovery.recover()` is implemented for durable committed-turn notices. E2 intentionally
does not invoke it because these `turn.context_prepared` messages are synthetic pre-dispatch work and
have no committed notice. The experiment therefore does not evaluate committed-notice recovery or
claim that the broader recovery path is absent.

### Test configuration and provenance

| Property | Value |
|---|---|
| Experiment ID | E2 |
| Event topic | `turn.context_prepared` |
| Events per scenario | 10 |
| Background worker | Not started |
| Normal scenario | Ten explicit `dispatch_one()` calls before close |
| Shutdown scenario | Close with all ten messages still queued |
| Python | CPython 3.12.4 |
| Platform | Windows 11 |
| Git commit | `173d2072af71d359579caa46210171add54794f0` |
| Working tree | Clean at experiment start (`working_tree_dirty=false`) |
| Execution | Serial; no other risk campaign running |
| Generated | November 17, 2026 at 01:20 UTC |

Raw per-event evidence is available in [E2_raw.csv](results/E2_raw.csv), and configuration, environment
and aggregate results are retained in [E2_summary.json](results/E2_summary.json).

### Results and comparison

| Measurement | Normal completion | Controlled shutdown with pending messages |
|---|---:|---:|
| Events submitted | 10 | 10 |
| Pending immediately before close | 0 | 10 |
| Events successfully dispatched | 10 | 0 |
| Events discarded during shutdown | 0 | 10 |
| Shutdown failure callbacks | 0 | 10 |
| Events restored in a fresh bus | 0 | 0 |
| Final original-bus queue depth | 0 | 0 |
| Fresh-bus initial queue depth | Not applicable | 0 |
| Close time | 0.0086 ms | 0.0410 ms |

The normal scenario accounts for all ten messages as successful dispatches. The queue was empty before
close, so closing it produced no failures. In the controlled-shutdown scenario, none of the ten handlers
executed. `close()` removed all ten queued messages and emitted one sanitized
`queue_closed_before_dispatch` callback for each. The original queue ended at depth zero, but that zero
means the messages were drained and discarded, not successfully processed.

The fresh bus also began at depth zero and contained none of the old message IDs. This is the expected
result for a new in-memory queue. The very short close duration is not evidence of successful delivery;
it reflects how quickly the current implementation removes the pending messages and invokes local error
callbacks.

### Design-specific versus architectural risk

This result is specific to the current design choices:

- `InProcessBus` stores messages only in process memory.
- `close()` deliberately drains undispatched messages instead of persisting or transferring them.
- A new bus creates a new empty queue.
- `CompletionRecovery.recover()` republishes durable committed notices, not arbitrary queued
  pre-commit messages such as the synthetic context events used here.

Event-Based architecture as a general style does not inherently lose pending events. It can use a
durable broker, transactional outbox, persistent consumer offsets, acknowledgement/redelivery policies
or an application-specific recovery log. E2 therefore identifies a risk in this project's present
in-memory connector and lifecycle policy, not a universal defect of event-driven systems.

### Limitations

- The experiment performs a controlled `close()`, not forced process termination or machine failure.
- Messages are synthetic intermediate events, not accepted turns or committed completion notices.
- No worker is started, making the pending/discarded boundary deterministic.
- The experiment does not test an in-flight handler or cancellation of a running network call.
- No durable repository or external broker participates.
- The implemented committed-notice recovery path is outside E2's scope; the experiment only observes
  whether a fresh bare bus restores these synthetic queued messages.

### Scenario summary and why this is a risk

E2 demonstrates complete accounting under both controlled conditions. When every queued message was
manually dispatched, all ten handlers executed and nothing was discarded. When the bus closed with ten
messages still pending, zero handlers executed, all ten messages were classified as discarded during
shutdown, and zero appeared in a fresh bus. The passing tests validate this accounting; they do not
convert discarded messages into recovered work.

This should be considered a risk because application shutdown can occur while valid operations are
waiting in the queue. Under the current design, error callbacks can report that the work was abandoned,
but they cannot make the queued message durable or restore it after restart. Although the application
persists turn attempts and recovers committed notices, E2 does not demonstrate reconstruction of an
abandoned pre-commit stage; explicit retry may still be required. The impact becomes more likely when
E1's single-worker bottleneck creates a larger or longer-lived backlog. Appropriate mitigations could
include graceful admission shutdown and queue draining, durable messaging, reconstruction from
persistent operation receipts and explicit retry semantics. Those mitigations were not evaluated by E2.

## L1 — Layered Architecture Synchronous AI Latency

### Short description

L1 measures how two sequential AI stages affect response time in the actual Layered coordination path.
It instantiates the real `LayeredApplication` and `LayeredGateway`, injects deterministic lightweight
collaborators to isolate coordinator latency, and submits a real `SubmitTurn` through
`LayeredGateway.submit()`.

The experiment is labeled **Layered coordinator simulation using controlled AI-service delays**. It
does not call an external provider, and its simulated commit does not prove SQLite durability.

The risk hypothesis is:

> Planning and narration run synchronously and sequentially in the caller's `submit()` path. The caller
> therefore waits for approximately the sum of both AI response times before commitment and before the
> gateway returns a completed receipt.

### Test procedure

1. Construct the real `LayeredApplication` and `LayeredGateway` for each trial.
2. Inject a synthetic authenticated owner and deterministic collaborators for gameplay preparation,
   validation, disclosure authorization, history, player projection, commit and reporting.
3. Use real `SubmitTurn`, `RequestContext`, `TurnAdmission`, `SessionSnapshot`, `TurnProposal`,
   `AcceptedTurn`, `CommittedTurnNotice`, `TurnCommit`, `PlayerView` and `TurnResultView` model types.
4. Configure the fake AI service with the same artificial delay in both `plan_turn()` and `narrate()`.
5. Execute one real `LayeredGateway.submit()` call with a unique request ID.
6. Record submission, planning start/finish, narration start/finish, simulated commit and gateway-return
   timestamps with `time.perf_counter_ns()`.
7. Record the complete collaborator call sequence, receipt status, number of AI calls, number of commits
   and reporting calls.
8. Test delays of 0, 0.5, 2, 4 and 6 seconds per AI call.
9. Repeat each delay ten times, producing 50 trials, 100 AI calls and 50 simulated commits.
10. Verify that planning finishes before narration begins, narration finishes before commit, and commit
    occurs before `submit()` returns.

Fast unit tests use zero or 1 ms delays and do not run the approximately 250-second benchmark during
normal test-suite execution.

### Test configuration and provenance

| Property | Value |
|---|---|
| Experiment ID | L1 |
| Label | Layered coordinator simulation using controlled AI-service delays |
| AI calls per trial | 2: planning followed by narration |
| Delays per AI call | 0, 0.5, 2, 4 and 6 seconds |
| Repetitions per delay | 10 |
| Total trials | 50 |
| Coordinator | Real `LayeredGateway` and `LayeredApplication` |
| Persistence | Deterministic fake; no durability claim |
| AI provider | Controlled fake; no network or external API |
| Python | CPython 3.12.4 |
| Platform | Windows 11 |
| Git commit | `173d2072af71d359579caa46210171add54794f0` |
| Working tree | Clean at experiment start (`working_tree_dirty=false`) |
| Execution | Serial; no other risk campaign running |
| Generated | November 17, 2026 at 01:24 UTC |

Raw trial evidence is available in [L1_raw.csv](results/L1_raw.csv), and aggregate statistics and
provenance are retained in [L1_summary.json](results/L1_summary.json).

### Results and comparison

All 50 submissions returned `completed`. Every trial made exactly two AI calls, performed exactly one
simulated commit and reported once. All 50 traces satisfied the required order:

```text
prepare -> plan start -> plan finish -> validate/project -> narrate start
        -> narrate finish -> commit -> report -> gateway return
```

| Delay per AI call | Expected combined delay | Planning median | Narration median | Response min | Response median | Response max |
|---:|---:|---:|---:|---:|---:|---:|
| 0 s | 0 s | 0.001 ms | 0.001 ms | 0.084 ms | 0.135 ms | 0.251 ms |
| 0.5 s | 1 s | 500.445 ms | 500.427 ms | 1,000.893 ms | 1,001.317 ms | 1,001.509 ms |
| 2 s | 4 s | 2,000.535 ms | 2,000.367 ms | 4,001.112 ms | 4,001.457 ms | 4,002.735 ms |
| 4 s | 8 s | 4,000.399 ms | 4,000.522 ms | 8,000.882 ms | 8,001.451 ms | 8,002.860 ms |
| 6 s | 12 s | 6,000.524 ms | 6,000.453 ms | 12,001.037 ms | 12,001.481 ms | 12,001.644 ms |

| Delay per call | Median submission-to-commit | Median non-AI overhead | Median response attributed to AI |
|---:|---:|---:|---:|
| 0 s | 0.109 ms | 0.133 ms | 1.704% |
| 0.5 s | 1,001.239 ms | 0.407 ms | 99.960% |
| 2 s | 4,001.380 ms | 0.472 ms | 99.987% |
| 4 s | 8,001.324 ms | 0.453 ms | 99.994% |
| 6 s | 12,001.352 ms | 0.468 ms | 99.996% |

The results closely match the sum of the two controlled delays. At 0.5 seconds per call, the median
response was approximately one second. At six seconds per call, it was approximately twelve seconds.
The non-AI coordinator overhead remained below one millisecond in the measured medians, so nearly all
latency in nonzero-delay conditions came from the two sequential simulated AI operations.

The gateway did not return a pending receipt while work continued elsewhere. It returned only after
planning, validation, narration, simulated commitment and reporting completed. This confirms the
synchronous latency propagation in the current Layered coordinator.

### Limitations

- The AI latency is produced by controlled `sleep()` calls, not an external provider.
- The simulated responses have no payload-generation, parsing, token or network cost.
- The fake commit is in memory and does not prove transaction atomicity or SQLite durability.
- Trials are sequential and do not measure concurrent Streamlit sessions or server capacity.
- The experiment measures successful calls only; provider timeouts, failures and uncertain late
  responses require separate scenarios.
- The result demonstrates coordinator behavior, not the response-time distribution of a selected AI
  model.

### Scenario summary and why this is a risk

L1 demonstrates that the current Layered workflow makes two AI operations sequentially inside a single
synchronous `submit()` call. Across all 50 trials, response time increased approximately as
`2 × configured AI delay`, and commitment did not occur until both calls finished. A six-second delay
per AI stage therefore produced a median gateway response of approximately 12.001 seconds.

This should be considered a responsiveness and availability risk because real AI latency is variable
and may be substantially higher than a controlled delay. A slow planner delays narration, and a slow
narrator delays commitment and the completed receipt. The design is straightforward and preserves a
clear ordered workflow, but the caller remains blocked throughout both remote operations. Potential
mitigations include asynchronous operation submission with polling, streaming progress, explicit
provider deadlines, cancellation and moving long-running work outside the request thread. These choices
would add operation-state, retry and recovery complexity and should be evaluated against the Layered
architecture's simplicity goals.

## L2 — Layered Reporting Delay and Failure

### Short description

L2 evaluates the performance and reliability consequences of calling reporting synchronously after a
gameplay commit in the Layered architecture. It uses the real `LayeredApplication` and `LayeredGateway`
with deterministic collaborators that isolate reporting behavior. The gameplay double maintains a
simple synthetic committed-state record, while the reporting double can complete immediately, complete
after a controlled delay or raise a controlled exception after commit.

The experiment is labeled **Layered coordinator/reporting simulation**. Its in-memory state record is
not actual SQLite persistence and does not prove durability.

L2 evaluates two related hypotheses:

> Performance: although gameplay has already committed, synchronous reporting delay is added directly
> to the caller's total response time.

> Reliability: if reporting raises after commit, the coordinator should preserve the accepted gameplay
> result, return a completed operation with `reporting_pending=True`, and avoid repeating the commit.

### Test procedure

1. Construct a real `LayeredApplication` and `LayeredGateway` for each isolated trial.
2. Reuse the deterministic account, provider-profile, knowledge, zero-delay AI and model fixture
   components from L1 without changing production code.
3. Use a gameplay double that records the commit timestamp, returns a valid `TurnCommit`, increments a
   commit counter and stores one synthetic committed-state entry keyed by operation ID.
4. Use a reporting double that records start and terminal timestamps and either succeeds, sleeps for a
   configured delay before succeeding, or raises a controlled exception immediately after commit.
5. Execute a real `SubmitTurn` through `LayeredGateway.submit()` with a unique request ID.
6. Run four scenarios:
   - A: reporting succeeds with no delay.
   - B: reporting succeeds after 0.5 seconds.
   - C: reporting succeeds after 2 seconds.
   - D: reporting raises immediately after the synthetic commit.
7. Repeat each scenario ten times, producing 40 trials.
8. Record submission-to-commit time, reporting duration, total response time, receipt status,
   `reporting_pending`, commit/report counts and the complete call sequence.
9. Verify that commit occurs before reporting starts and that every trial commits exactly once.
10. For failure trials, verify that the synthetic committed-state entry remains present and that the
    gateway operation remains `completed`.

`ReportingService.catch_up()` is implemented, but recovery was not invoked or evaluated by L2. The
separate gameplay integration tests exercise durable pending-notice catch-up after restart without
repeating provider calls.

### Test configuration and provenance

| Property | Value |
|---|---|
| Experiment ID | L2 |
| Label | Layered coordinator/reporting simulation |
| Scenarios | Normal, 0.5 s delay, 2 s delay, immediate failure after commit |
| Repetitions per scenario | 10 |
| Total trials | 40 |
| Coordinator | Real `LayeredGateway` and `LayeredApplication` |
| Gameplay state | Synthetic in-memory record; no durability claim |
| Reporting | Configurable deterministic double |
| AI | Zero-delay deterministic double reused from L1 |
| Python | CPython 3.12.4 |
| Platform | Windows 11 |
| Git commit | `173d2072af71d359579caa46210171add54794f0` |
| Working tree | Clean at experiment start (`working_tree_dirty=false`) |
| Execution | Serial; no other risk campaign running |
| Generated | November 17, 2026 at 01:25 UTC |

The L2 summary records SHA-256 digests for both the L2 script and the reused L1 fixture module. Raw
trial evidence is available in [L2_raw.csv](results/L2_raw.csv), and aggregate statistics and provenance
are retained in [L2_summary.json](results/L2_summary.json).

### Performance results and comparison

| Scenario | Reporting median | Response minimum | Response median | Response maximum | Median increase vs. normal |
|---|---:|---:|---:|---:|---:|
| A: no delay, success | 0.001 ms | 0.067 ms | 0.096 ms | 0.188 ms | 0 ms |
| B: 0.5 s delay, success | 500.394 ms | 500.471 ms | 500.782 ms | 553.354 ms | 500.686 ms |
| C: 2 s delay, success | 2,000.522 ms | 2,000.589 ms | 2,000.941 ms | 2,002.125 ms | 2,000.845 ms |
| D: immediate failure | 0.004 ms | 0.274 ms | 0.452 ms | 0.985 ms | 0.357 ms |

The controlled reporting delay was added almost one-for-one to the gateway's response time. In scenario
B, a 0.5-second reporting delay increased median response time by approximately 500.7 ms. In scenario
C, a 2-second delay increased it by approximately 2,000.8 ms. Gameplay had already reached the
synthetic commit point, but `submit()` still waited for reporting to return.

### Reliability results and comparison

| Scenario | Completed receipts | Commits preserved | Commit exactly once | Reporting successes | Reporting failures | `reporting_pending=True` |
|---|---:|---:|---:|---:|---:|---:|
| A: no delay, success | 10 / 10 | 10 / 10 | 10 / 10 | 10 | 0 | 0 |
| B: 0.5 s delay, success | 10 / 10 | 10 / 10 | 10 / 10 | 10 | 0 | 0 |
| C: 2 s delay, success | 10 / 10 | 10 / 10 | 10 / 10 | 10 | 0 | 0 |
| D: immediate failure | 10 / 10 | 10 / 10 | 10 / 10 | 0 | 10 | 10 |

All 40 traces recorded commit before reporting. Successful reporting returned
`reporting_pending=False`. In all ten failure trials, the reporting exception occurred after the
synthetic commit, but the gateway still returned `completed`, the accepted `TurnResultView` remained
available with `reporting_pending=True`, the committed-state record remained present and commit was not
repeated.

The existing coordinator therefore isolates immediate reporting failure from the gameplay result as
intended. This is evidence about control flow and the synthetic record only; it is not evidence that a
real SQLite transaction or pending notification survives process failure.

### Limitations

- Gameplay, AI and reporting collaborators are deterministic test doubles.
- The synthetic committed-state dictionary is not durable storage.
- Reporting delay uses `sleep()` and does not include SQL queries, aggregation or external systems.
- The experiment does not test process restart, notification persistence, reporting deduplication or
  milestone correctness.
- Only immediate post-commit reporting failure is injected; partial reporting writes are not modeled.
- The implemented `ReportingService.catch_up()` path is outside this experiment, so L2 does not measure
  restart recovery time or eventual projection freshness.

### Scenario summary and why this is a risk

L2 demonstrates both a performance cost and an existing reliability safeguard. On the performance side,
reporting is invoked synchronously after gameplay commitment, so reporting latency remains on the
player-facing critical path. The 0.5-second and 2-second reporting delays increased median gateway
response time by approximately the same amounts even though the gameplay result was already committed.
Slow reporting can therefore make accepted gameplay appear slow or unresponsive.

On the reliability side, the Layered coordinator correctly caught all ten controlled reporting
exceptions after commit. It returned completed gameplay results with `reporting_pending=True`, retained
the synthetic committed-state entry and did not repeat the commit. The production implementation also
has durable pending notices and an idempotent `catch_up()` path, and the broader integration suite tests
restart catch-up without another provider call. L2 itself does not measure that recovery path, so its
reliability conclusion is limited to immediate post-commit failure isolation. The remaining concerns are
temporary projection staleness until catch-up runs and the lack of a recovery-time measurement. Keeping
reporting outside the response-critical path and exposing projection freshness would further reduce
those effects.

## P1 — Cross-Architecture Private-Data Isolation

### Short description

P1 tests whether private information crosses an interface where it is not allowed. It executes a
complete turn through each production gateway with a deterministic recording provider. The campaign
fixture contains a tagged director-only fact that should be available to planning but not narration or
player-facing outputs. A separate tagged credential is supplied through the in-memory credential vault
and must not appear in provider messages, receipts, queries or Event-Based traces.

The risk hypothesis is:

> A broad planning context or queued workflow could accidentally carry director-only story data or a
> provider credential into narration, public results, diagnostic traces or queries.

The experiment records boolean exposure evidence only. It deliberately omits the canary values and
provider message contents from the CSV and summary artifacts.

### Test procedure

1. Build an isolated Layered or Event-Based application using the production gateway, domain services
   and SQLite repositories. The Event-Based worker is disabled so dispatch is deterministic.
2. Register and authenticate an account, store and activate a provider card with a tagged credential,
   publish the Thornmere fixture and start a session as Rowan.
3. Submit the same no-effect turn through each architecture. The recording provider returns a valid
   empty plan and safe narration.
4. Verify that the director-only fixture canary is present in the planner request. This positive control
   proves the canary actually reached the intended private planning boundary.
5. Verify that the private canary is absent from the narrator request, operation receipt/result, resumed
   player view, campaign report, provider-card query, Event-Based trace and subscriber-failure metadata.
6. Verify that the credential canary is absent from all provider messages and public outputs.
7. Repeat three times per architecture, producing six trials and twelve provider calls.

### Test configuration and provenance

| Property | Value |
|---|---|
| Experiment ID | P1 |
| Architectures | Layered and Event-Based production gateways |
| Repetitions | 3 per architecture; 6 total |
| Provider | Deterministic recording double; exact request messages inspected in memory |
| Private positive control | Director-only fixture record reaches planning |
| Negative boundaries | Narration, receipts/results, queries, traces and failure metadata |
| Credential check | Tagged in-memory credential absent from provider messages and public outputs |
| Persistence | Production SQLite repositories in isolated temporary directories |
| Python | CPython 3.12.4 |
| Platform | Windows 11 |
| Git commit | `173d2072af71d359579caa46210171add54794f0` |
| Working tree | Clean at experiment start (`working_tree_dirty=false`) |
| Execution | Serial; no other risk campaign running |
| Generated | November 17, 2026 at 01:25 UTC |

Raw boolean evidence is available in [P1_raw.csv](results/P1_raw.csv), and aggregate results and source
digests are retained in [P1_summary.json](results/P1_summary.json).

### Results and comparison

| Architecture | Trials passed | Planner received private canary | Narrator exposures | Public-output exposures | Trace/failure exposures | Credential exposures |
|---|---:|---:|---:|---:|---:|---:|
| Layered | 3 / 3 | 3 / 3 | 0 | 0 | 0 | 0 |
| Event-Based | 3 / 3 | 3 / 3 | 0 | 0 | 0 | 0 |

All six trials passed. The private canary reached the planner in every trial, so the absence from later
boundaries is meaningful rather than the result of a missing test input. Neither architecture sent the
director-only canary to narration or exposed it through the inspected player-facing and diagnostic
boundaries. The credential canary appeared in none of the inspected provider message content or public
outputs.

### Limitations and risk assessment

- P1 checks one known director-only record and one credential canary, not every private field.
- It uses deterministic responses and does not send a network request to an external provider.
- In-memory inspection covers the current provider-message model, results, selected queries and bus
  trace; it is not a general information-flow proof for future code.
- Log aggregation, crash dumps, third-party SDK telemetry and the Streamlit server process are outside
  the experiment.
- Three repetitions establish deterministic boundary behavior, not a probabilistic leak rate.

P1 did not reproduce the proposed private-data leak in either architecture. This is positive evidence
for the current typed planning/narration split, player projections, credential capture and metadata-only
Event-Based tracing. Privacy remains a regression risk: new fields, diagnostic logging or provider
adapters must preserve these negative assertions.

## R1 — Commit-Then-Interrupt Restart Recovery

### Short description

R1 tests the durable boundary that E2 and L2 intentionally did not cover. It performs a real turn with
the production SQLite repository, interrupts processing immediately after the atomic commit creates a
pending committed notice and before either result or reporting consumer acknowledges it, then rebuilds
the application from the same database.

The risk hypothesis is:

> An interruption after gameplay commits but before notification consumers acknowledge could lose the
> accepted result, repeat local effects, repeat paid provider calls or leave reporting permanently stale.

### Test procedure

1. Seed an isolated production application and submit a deterministic two-call turn.
2. In Layered, inject a controlled exception into the first result acknowledgement. This occurs after
   the SQLite commit and before result or reporting acknowledgement; the volatile gateway receipt is
   therefore `failed` even though durable gameplay has committed.
3. In Event-Based, dispatch through the persistence handler until the database contains the committed
   notice, then close before dispatching the queued `turn.committed` event. The volatile receipt remains
   `running` at interruption.
4. Before restart, verify one pending notice, zero consumer acknowledgements and two provider calls.
5. Rebuild the same architecture against the same database. Layered performs startup result/report
   catch-up; Event-Based republishes the committed identifier and deterministically dispatches it.
6. Measure rebuild-through-catch-up time and verify zero pending notices, two consumer acknowledgements,
   session revision one, one accepted turn, a report containing one accepted turn and no additional
   provider calls.
7. Repeat three times per architecture.

### Test configuration and provenance

| Property | Value |
|---|---|
| Experiment ID | R1 |
| Architectures | Layered and Event-Based production gateways |
| Repetitions | 3 per architecture; 6 total |
| Interruption boundary | After SQLite commit; before result/report acknowledgements |
| Provider | Deterministic recording double; two calls before interruption |
| Persistence | Production SQLite repository and pending-notification/consumer-receipt tables |
| Recovery measure | Application rebuild through completed catch-up |
| Python | CPython 3.12.4 |
| Platform | Windows 11 |
| Git commit | `173d2072af71d359579caa46210171add54794f0` |
| Working tree | Clean at experiment start (`working_tree_dirty=false`) |
| Execution | Serial; no other risk campaign running |
| Generated | November 17, 2026 at 01:25 UTC |

Raw per-trial evidence is available in [R1_raw.csv](results/R1_raw.csv), and aggregate results and source
digests are retained in [R1_summary.json](results/R1_summary.json).

### Results and comparison

| Measurement | Layered | Event-Based |
|---|---:|---:|
| Trials passed | 3 / 3 | 3 / 3 |
| Volatile status at interruption | `failed` | `running` |
| Pending notices before → after restart | 1 → 0 per trial | 1 → 0 per trial |
| Consumer acknowledgements before → after | 0 → 2 per trial | 0 → 2 per trial |
| Accepted turns after restart | 1 per trial | 1 per trial |
| Session revision after restart | 1 per trial | 1 per trial |
| Provider calls before → after restart | 2 → 2 per trial | 2 → 2 per trial |
| Recovery time minimum | 24.627 ms | 22.362 ms |
| Recovery time median | 24.657 ms | 23.803 ms |
| Recovery time maximum | 25.533 ms | 28.519 ms |

All six trials recovered the durable commit exactly once from the experiment's observable perspective:
one accepted history entry, one state revision, a one-turn report and no provider replay. Both consumers
acknowledged the same durable notice after restart. The different interruption statuses are important:
an in-memory `failed` or `running` status is not authoritative once the commit boundary may have been
crossed. Durable operation/turn state must be consulted before retrying.

### Limitations and risk assessment

- The interruption is a deterministic fault injection, not an operating-system process kill or power
  failure.
- Recovery uses local SQLite without concurrent writers, filesystem failure or database corruption.
- Three repetitions provide a correctness demonstration and a local recovery-time observation, not a
  production recovery-time distribution or service objective.
- The experiment interrupts before both acknowledgements; it does not inject every partial-consumer or
  partial-reporting-write combination.
- R1 verifies the persisted turn, acknowledgements, history, report count and provider-call count. It
  does not prove all future effects are idempotent.

R1 demonstrates that the current committed-notice design closes the principal post-commit recovery gap
identified by E2 and L2: accepted state and notices survive restart, both consumers catch up, and paid AI
work is not replayed. Residual risks remain around partial writes outside the transaction, concurrent
recovery, corrupted storage and operational observability.

## Cross-Experiment Risk Analysis and Final Assessment

### Overall summary

The six experiments evaluate failure, delay, privacy and recovery mechanisms in the two architectural
implementations. E1 and E2 isolate the Event-Based queue connector. L1 and L2 isolate Layered latency
and reporting behavior. P1 and R1 run matched completed workflows through both production gateways and
SQLite repositories. Together, they show both shared safeguards and different operational risks.

| Experiment | Architecture and concern | Principal observed evidence | Risk demonstrated |
|---|---|---|---|
| E1 | Event-Based: single-worker queue | A 0.5 s handler limited service to 1.998 events/s; p95 queue wait reached 9.010 s for a 20-event burst | Head-of-line blocking, latency amplification and backlog growth |
| E2 | Event-Based: controlled shutdown | 10 queued events produced 0 dispatches, 10 shutdown discards and 0 restorations in a fresh bus | Pending work is nondurable under the current queue lifecycle |
| L1 | Layered: two synchronous AI stages | A 6 s delay in each AI call produced a 12.001 s median `submit()` response | Sequential external-service latency is passed directly to the caller |
| L2 | Layered: synchronous reporting | A 2 s reporting delay added 2.001 s to median response; 10/10 reporting failures preserved one commit and returned `reporting_pending=True` | Reporting adds critical-path latency; immediate failure is isolated, while catch-up is covered outside L2 |
| P1 | Both: private boundary isolation | 6/6 trials passed; planner positive controls succeeded with zero narrator, public, trace or credential exposures | Current typed projections and metadata-only traces prevented the tested leaks |
| R1 | Both: interrupt after commit | 6/6 trials recovered one turn and two acknowledgements with provider calls unchanged at two | Durable committed notices recover without replaying paid AI work |

All six experiment harnesses passed their correctness assertions. That result means the measurements
are internally accounted for: messages and trials were unique, expected calls occurred, ordering was
checked and no result was fabricated. Passing the harnesses does not mean the measured architectural
behavior is risk-free. In several cases the risk is precisely the behavior that the passing test
reproduced.

### Event-Based architecture findings

E1 confirms that the current Event-Based connector decouples publication from handler execution, but
all handling still depends on one sequential worker. When service time is nontrivial, later independent
events wait behind earlier ones. The measured 0.5-second handler created approximately 9 seconds of p95
queue wait and approximately 10 seconds of total burst time. If sustained arrival exceeds the worker's
service rate, the unbounded queue has no mechanism to stop backlog growth.

E2 confirms what happens if that backlog is still present during controlled shutdown. The current
`close()` implementation removes queued messages and reports them through the shutdown callback, but it
does not persist or transfer them. A new bus begins empty. Error notification provides useful local
accounting, but it is not recovery.

These results form a compound risk chain:

```text
slow handler or traffic burst
    -> single-worker backlog
    -> longer period with pending in-memory work
    -> shutdown while work is pending
    -> queued messages discarded without restoration
```

E1 does not prove that E2 will occur, and E2 is not a crash test. However, E1 increases the duration and
quantity of pending work that would be exposed to E2's lifecycle behavior. The combined evidence makes
queue scheduling, shutdown admission and message durability higher priorities than either experiment
would suggest in isolation.

R1 establishes the boundary on that conclusion. Once a turn has atomically committed and created its
durable notice, Event-Based restart recovery republishes the identifier, both consumers acknowledge it,
the accepted effect is not duplicated and the provider is not called again. E2's loss applies to queued
pre-commit messages, not committed notices.

The risks are specific to the project's current connector choices, not to Event-Based architecture in
general. Event-driven systems can use multiple workers, partitioning, bounded queues, backpressure,
durable brokers, acknowledgements, transactional outboxes and redelivery. Adding those mechanisms would
also introduce ordering, idempotency, session-serialization and operational complexity that the current
implementation does not yet address.

### Layered architecture findings

L1 confirms the cost of explicit synchronous coordination. The ordered gameplay path is easy to trace,
but the player-facing call waits for both AI stages. Across 50 trials, response time closely followed the
sum of planning and narration delays. The coordinator added very little overhead; the risk comes from
placing two variable-latency external operations sequentially on the critical path.

L2 shows that reporting is also on that synchronous path, even though it runs after gameplay commitment.
A 0.5-second reporting delay added approximately 0.5 seconds to the gateway response, and a 2-second
delay added approximately 2 seconds. If AI and reporting are both slow during one real operation, their
latencies would be encountered sequentially according to the coordinator code. The experiments did not
run those delays together, so this report does not claim a measured combined value.

L2 also demonstrates an important strength: the coordinator's post-commit exception boundary prevented
reporting failure from being mislabeled as gameplay failure. All ten injected reporting failures returned
completed gameplay results, set `reporting_pending=True`, preserved the synthetic state record and did
not repeat the commit. This reduces immediate failure coupling between gameplay and analytics.

R1 complements L2 with direct recovery evidence. Layered startup catch-up cleared both outstanding
consumer acknowledgements in all three trials, retained one accepted turn and made no additional
provider call. The local median recovery observation was 24.657 ms. This reduces the residual reliability
risk to temporary reporting lag, partial-write cases outside R1 and recovery observability rather than a
missing catch-up mechanism.

### Shared privacy and recovery findings

P1 found no private-canary or credential exposure in either architecture across the inspected narration,
public result/query and diagnostic boundaries. The positive planning control succeeded in every trial,
so the negative results exercise a real private value. R1 likewise showed the same durable outcome in
both styles despite different volatile statuses at interruption. These results support shared domain and
persistence safeguards; they do not erase the architectures' distinct latency and queue-lifecycle risks.

### Comparative risk view

| Dimension | Current Event-Based design | Current Layered design |
|---|---|---|
| Request behavior | Publication can return before handling, but work waits on one worker | Caller remains inside the complete ordered workflow |
| Slow external work | Can block all later events on the worker and grow the queue | Directly increases the initiating caller's response time |
| Failure visibility | Shutdown callback identifies discarded queued work | Gateway returns a completed result with reporting marked pending |
| Durability demonstrated | E2: none for queued pre-commit messages; R1: committed state and notices survive restart | R1: committed state and notices survive restart |
| Recovery demonstrated | R1: 3/3 recovered with two acknowledgements and no provider replay | R1: 3/3 recovered with two acknowledgements and no provider replay |
| Private isolation demonstrated | P1: 3/3 with zero tested exposures | P1: 3/3 with zero tested exposures |
| Current strength | Indirect publication, subscriber isolation and durable committed-notice replay | Explicit sequencing, post-commit failure isolation and synchronous startup catch-up |
| Principal current risk | Backlog plus nondurable queued pre-commit events | Accumulated synchronous latency plus temporary reporting lag after failure |

The experiments do not establish that one architecture is universally superior. P1 and R1 use matched
completed workflows, but E1/L1 and E2/L2 intentionally isolate different mechanisms rather than provide
a matched throughput benchmark. The evidence still shows the anticipated tradeoff: Layered makes
ordering and failure boundaries explicit but exposes callers to sequential latency, while Event-Based
can separate submission from processing but requires deliberate queue capacity, pre-commit durability
and concurrency policies.

### Risk priority and recommended actions

The following priorities follow from the observed consequences rather than from an attempt to redesign
the project inside the experiments.

1. **Define measurable service objectives and operational metrics.** Record queue depth, queue wait,
   provider latency, reporting lag, operation age and incomplete-operation counts. Without thresholds,
   the system can observe delay but cannot determine when to reject, shed or escalate work.
2. **Clarify and extend the durability boundary.** Durable committed notices, consumer acknowledgements
   and restart recovery now exist. Decide which admitted pre-commit operations also require persistence,
   and document which work must be explicitly retried after queue shutdown.
3. **Choose and test an Event-Based capacity policy.** Consider bounded admission, backpressure,
   workload-specific queues or carefully partitioned workers. Preserve per-session ordering and verify
   idempotency before adding concurrency.
4. **Keep reporting outside the player-critical path where practical.** The L2 failure boundary already
   treats reporting as secondary to committed gameplay. Background processing should retain that
   semantic separation and reuse the existing durable notification and idempotent catch-up behavior.
5. **Bound Layered external-call latency.** Apply explicit provider deadlines, cancellation and visible
   operation status. If long-running turns become asynchronous, retain the Layered workflow's validation,
   commit ordering and explicit retry rules.
6. **Expand recovery fault injection beyond R1.** R1 covers interruption after commit and before both
   acknowledgements. Add partial-consumer completion, partial reporting writes, concurrent startup and
   storage-failure cases, and define a recovery-time objective.
7. **Run a matched end-to-end comparison.** Use the same campaign, commands, scripted provider responses
   and persistence semantics through both completed gateways. This is required before drawing
   end-to-end performance or architecture-selection conclusions.

### Final thought

The experiments reveal risks rather than defects in the architectural styles themselves. The most
significant Event-Based exposure is the combination of a single unbounded worker queue and no durability
for queued pre-commit messages: slow work can accumulate, and that pending work is discarded during the
tested controlled shutdown. Durable committed-notice recovery exists, but it begins after the boundary
tested by E2; R1 confirms that recovery succeeds after that boundary.
The most significant Layered exposure is cumulative synchronous latency: planning, narration and
reporting all delay the caller in their defined order. The Layered coordinator does, however, already
contain a useful reliability boundary that prevents reporting failure from reversing or falsely failing
an accepted synthetic turn. P1 and R1 add shared positive evidence: the inspected privacy boundaries did
not leak their canaries, and committed turns recovered without provider replay in both architectures.

At the current MVP stage, the evidence favors strengthening both implementations rather than declaring
a winner. The Event-Based version needs an explicit capacity policy and a documented durability boundary
for admitted pre-commit work. The Layered version needs latency bounds and removal of secondary reporting
work from the response path while preserving its clear commit-before-reporting rule. Final architectural
judgment should be deferred until the same end-to-end workload is measured through both completed
gateways.
