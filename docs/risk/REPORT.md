# Architectural Risk Experiment Report

This report collects the four architectural-risk experiments for the storytelling project. Each
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

## E1 — Single-Worker Queue Bottleneck

### Short description

E1 evaluates how the existing Event-Based connector behaves when a burst of independent events is
handled by one worker. The production `InProcessBus` has an unbounded in-memory queue and one worker
that invokes subscribers sequentially. The experiment does not use unfinished gameplay handlers. It
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
| Git commit | `1bd8790ae29a01007c55b41dc3ef76c396b065df` |
| Working tree | Dirty; the uncommitted E1 files and results were included in the run |
| Generated | November 13, 2026 at 19:56 UTC |

The experiment script's SHA-256 digest is retained in the summary JSON so that the results can be
matched to the exact measured script. The fixed commit SHA alone is insufficient provenance while the
experiment remains uncommitted.

### Results and comparison

All 120 events completed, all 120 event IDs were unique, every handler was invoked exactly once, and
the bus reported no errors. Therefore, the observed delay is not caused by message loss, duplicate
handling or failed processing.

| Measurement | Baseline: 0.0 s delay | Slow: 0.5 s delay |
|---|---:|---:|
| Completed events | 60 / 60 | 60 / 60 |
| Incomplete events | 0 | 0 |
| Bus errors | 0 | 0 |
| Mean elapsed time per 20-event burst | 0.466 ms | 10,009.509 ms |
| Mean queue waiting time | 0.226 ms | 4,754.568 ms |
| P95 queue waiting time | 0.290 ms | 9,009.217 ms |
| Maximum queue waiting time | 0.304 ms | 9,509.662 ms |
| Mean handler processing time | 0.003 ms | 500.382 ms |
| P95 end-to-end event latency | 0.293 ms | 9,509.604 ms |
| Maximum end-to-end event latency | 0.306 ms | 10,009.954 ms |
| Mean observed throughput | 44,353.936 events/s | 1.998 events/s |
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
9.009 seconds p95 waiting / 0.5 seconds processing = approximately 18 times
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
> queued messages and reports shutdown failures, but a new bus cannot restore them because no durable
> queue or completed recovery implementation exists.

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

`CompletionRecovery.recover()` was inspected before the experiment. It remains explicitly unfinished
and raises the `EVENTS` not-implemented error. It was not invoked, and no recovery result was
fabricated.

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
| Git commit | `1bd8790ae29a01007c55b41dc3ef76c396b065df` |
| Working tree | Dirty; uncommitted experiment/report files were included |
| Generated | November 13, 2026 at 20:32 UTC |

Raw per-event evidence is available in [E2_raw.csv](results/E2_raw.csv), and configuration, environment
and aggregate results are retained in [E2_summary.json](results/E2_summary.json).

### Results and comparison

| Measurement | Normal completion | Controlled shutdown with pending messages |
|---|---:|---:|
| Events submitted | 10 | 10 |
| Pending immediately before dispatch/close | 0 after dispatch | 10 |
| Events successfully dispatched | 10 | 0 |
| Events discarded during shutdown | 0 | 10 |
| Shutdown failure callbacks | 0 | 10 |
| Events restored in a fresh bus | 0 | 0 |
| Final original-bus queue depth | 0 | 0 |
| Fresh-bus initial queue depth | Not applicable | 0 |
| Close time | 0.0072 ms | 0.0298 ms |

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
- `CompletionRecovery.recover()` is unfinished.

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
- Because recovery is unfinished, E2 can only observe the empty fresh bus; it cannot evaluate a future
  recovery implementation.

### Scenario summary and why this is a risk

E2 demonstrates complete accounting under both controlled conditions. When every queued message was
manually dispatched, all ten handlers executed and nothing was discarded. When the bus closed with ten
messages still pending, zero handlers executed, all ten messages were classified as discarded during
shutdown, and zero appeared in a fresh bus. The passing tests validate this accounting; they do not
convert discarded messages into recovered work.

This should be considered a risk because application shutdown can occur while valid operations are
waiting in the queue. Under the current design, error callbacks can report that the work was abandoned,
but they cannot make it durable or restore it after restart. Users may need to resubmit operations, and
without durable request state the application may not know whether work was never started, partially
executed elsewhere or safe to retry. The impact becomes more likely when E1's single-worker bottleneck
creates a larger or longer-lived backlog. Appropriate mitigations could include graceful admission
shutdown and queue draining, durable messaging, persistent operation receipts, an outbox/recovery path
and explicit retry semantics. Those mitigations were intentionally not implemented by E2.

## L1 — Layered Architecture Synchronous AI Latency

### Short description

L1 measures how two sequential AI stages affect response time in the actual Layered coordination path.
It instantiates the real `LayeredApplication` and `LayeredGateway`, injects deterministic lightweight
collaborators for unfinished business services, and submits a real `SubmitTurn` through
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
| Git commit | `1bd8790ae29a01007c55b41dc3ef76c396b065df` |
| Working tree | Dirty; uncommitted risk experiments and results were included |
| Generated | November 13, 2026 at 20:45 UTC |

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
| 0 s | 0 s | 0.001 ms | 0.001 ms | 0.039 ms | 0.045 ms | 0.197 ms |
| 0.5 s | 1 s | 500.514 ms | 500.323 ms | 1,000.820 ms | 1,001.085 ms | 1,001.523 ms |
| 2 s | 4 s | 2,000.416 ms | 2,000.411 ms | 4,000.636 ms | 4,001.193 ms | 4,001.899 ms |
| 4 s | 8 s | 4,000.447 ms | 4,000.453 ms | 8,001.126 ms | 8,001.265 ms | 8,001.381 ms |
| 6 s | 12 s | 6,000.532 ms | 6,000.546 ms | 12,001.060 ms | 12,001.334 ms | 12,002.005 ms |

| Delay per call | Median submission-to-commit | Median non-AI overhead | Median response attributed to AI |
|---:|---:|---:|---:|
| 0 s | 0.038 ms | 0.044 ms | 2.889% |
| 0.5 s | 1,001.024 ms | 0.265 ms | 99.974% |
| 2 s | 4,001.121 ms | 0.325 ms | 99.992% |
| 4 s | 8,001.200 ms | 0.406 ms | 99.995% |
| 6 s | 12,001.235 ms | 0.426 ms | 99.997% |

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
with deterministic collaborators for unfinished business services. The gameplay double maintains a
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

`ReportingService.catch_up()` was inspected before execution and remains explicitly unfinished.
Recovery was not invoked or evaluated.

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
| Git commit | `1bd8790ae29a01007c55b41dc3ef76c396b065df` |
| Working tree | Dirty; uncommitted experiments and results were included |
| Generated | November 13, 2026 at 20:53 UTC |

The L2 summary records SHA-256 digests for both the L2 script and the reused L1 fixture module. Raw
trial evidence is available in [L2_raw.csv](results/L2_raw.csv), and aggregate statistics and provenance
are retained in [L2_summary.json](results/L2_summary.json).

### Performance results and comparison

| Scenario | Reporting median | Response minimum | Response median | Response maximum | Median increase vs. normal |
|---|---:|---:|---:|---:|---:|
| A: no delay, success | 0.001 ms | 0.035 ms | 0.045 ms | 0.128 ms | 0 ms |
| B: 0.5 s delay, success | 500.571 ms | 500.329 ms | 500.827 ms | 501.039 ms | 500.782 ms |
| C: 2 s delay, success | 2,000.419 ms | 2,000.564 ms | 2,000.697 ms | 2,001.055 ms | 2,000.651 ms |
| D: immediate failure | 0.003 ms | 0.077 ms | 0.251 ms | 0.344 ms | 0.205 ms |

The controlled reporting delay was added almost one-for-one to the gateway's response time. In scenario
B, a 0.5-second reporting delay increased median response time by approximately 500.8 ms. In scenario
C, a 2-second delay increased it by approximately 2,000.7 ms. Gameplay had already reached the
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
- `ReportingService.catch_up()` remains unfinished, so recovery and eventual projection freshness were
  not evaluated.

### Scenario summary and why this is a risk

L2 demonstrates both a performance cost and an existing reliability safeguard. On the performance side,
reporting is invoked synchronously after gameplay commitment, so reporting latency remains on the
player-facing critical path. The 0.5-second and 2-second reporting delays increased median gateway
response time by approximately the same amounts even though the gameplay result was already committed.
Slow reporting can therefore make accepted gameplay appear slow or unresponsive.

On the reliability side, the Layered coordinator correctly caught all ten controlled reporting
exceptions after commit. It returned completed gameplay results with `reporting_pending=True`, retained
the synthetic committed-state entry and did not repeat the commit. However, the pending marker alone
does not provide recovery: the real reporting service, durable notification repository and `catch_up()`
path remain unfinished. The residual risk is that analytics and milestone projections can remain stale
after a reporting failure, with no demonstrated mechanism to catch up after restart. Potential
mitigations include keeping reporting outside the response-critical path, durable pending notices,
idempotent reporting updates, projection-freshness indicators and an implemented catch-up process.

## Cross-Experiment Risk Analysis and Final Assessment

### Overall summary

The four experiments evaluate different failure and delay mechanisms in the two architectural
implementations. E1 and E2 exercise the real Event-Based queue connector. L1 and L2 exercise the real
Layered gateway and application coordinator with deterministic substitutes for unfinished services.
Together, they show that both designs preserve some intended structural properties while exposing
different operational risks.

| Experiment | Architecture and concern | Principal observed evidence | Risk demonstrated |
|---|---|---|---|
| E1 | Event-Based: single-worker queue | A 0.5 s handler limited service to 1.998 events/s; p95 queue wait reached 9.009 s for a 20-event burst | Head-of-line blocking, latency amplification and backlog growth |
| E2 | Event-Based: controlled shutdown | 10 queued events produced 0 dispatches, 10 shutdown discards and 0 restorations in a fresh bus | Pending work is nondurable under the current queue lifecycle |
| L1 | Layered: two synchronous AI stages | A 6 s delay in each AI call produced a 12.001 s median `submit()` response | Sequential external-service latency is passed directly to the caller |
| L2 | Layered: synchronous reporting | A 2 s reporting delay added 2.001 s to median response; 10/10 reporting failures preserved one commit and returned `reporting_pending=True` | Reporting adds critical-path latency; immediate failure is isolated, but recovery is unproved |

All four experiment harnesses passed their correctness assertions. That result means the measurements
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

The remaining reliability concern is recovery rather than immediate isolation. A pending flag informs
the caller that reporting is behind, but `ReportingService.catch_up()`, durable pending notices and the
repository implementations are unfinished. The current evidence therefore supports the claim that the
control flow preserves an accepted synthetic result; it does not support a claim that reporting will
eventually recover after restart.

### Comparative risk view

| Dimension | Current Event-Based design | Current Layered design |
|---|---|---|
| Request behavior | Publication can return before handling, but work waits on one worker | Caller remains inside the complete ordered workflow |
| Slow external work | Can block all later events on the worker and grow the queue | Directly increases the initiating caller's response time |
| Failure visibility | Shutdown callback identifies discarded queued work | Gateway returns a completed result with reporting marked pending |
| Durability demonstrated | None for the in-memory queue; fresh bus restored 0 events | None for fake commits; SQLite repositories remain unfinished |
| Recovery demonstrated | No; `CompletionRecovery.recover()` is unfinished | No; `ReportingService.catch_up()` is unfinished |
| Current strength | Indirect publication and subscriber failure isolation | Explicit sequencing and post-commit reporting-failure isolation |
| Principal current risk | Backlog plus nondurable pending events | Accumulated synchronous latency plus stale reporting after failure |

The experiments do not establish that one architecture is universally superior. They used different
controlled scenarios and did not run functionally equivalent completed gameplay implementations under
the same workload. They do show the practical tradeoff anticipated by the project proposal: the Layered
style makes ordering and failure boundaries explicit but exposes callers to sequential latency, while the
Event-Based style can separate submission from processing but requires deliberate queue capacity,
durability, recovery and concurrency policies.

### Risk priority and recommended actions

The following priorities follow from the observed consequences rather than from an attempt to redesign
the project inside the experiments.

1. **Define measurable service objectives and operational metrics.** Record queue depth, queue wait,
   provider latency, reporting lag, operation age and incomplete-operation counts. Without thresholds,
   the system can observe delay but cannot determine when to reject, shed or escalate work.
2. **Make admitted work and post-commit notices durable.** Implement durable request receipts, pending
   completion notices, consumer acknowledgements and restart recovery before claiming reliable delivery.
   Recovery must redeliver committed notifications without automatically replaying paid AI requests.
3. **Choose and test an Event-Based capacity policy.** Consider bounded admission, backpressure,
   workload-specific queues or carefully partitioned workers. Preserve per-session ordering and verify
   idempotency before adding concurrency.
4. **Keep reporting outside the player-critical path where practical.** The L2 failure boundary already
   treats reporting as secondary to committed gameplay. Background processing should retain that
   semantic separation while adding durable notification and projection-freshness handling.
5. **Bound Layered external-call latency.** Apply explicit provider deadlines, cancellation and visible
   operation status. If long-running turns become asynchronous, retain the Layered workflow's validation,
   commit ordering and explicit retry rules.
6. **Implement and test recovery paths.** Complete `CompletionRecovery.recover()` and
   `ReportingService.catch_up()`, then inject interruption after commit and verify eventual delivery,
   deduplication and zero repeated provider calls.
7. **Repeat the experiments after business implementation.** Use the same campaign, commands, scripted
   provider responses and persistence semantics through both gateways. This is required before drawing
   end-to-end performance or architecture-selection conclusions.

### Final thought

The experiments reveal risks rather than defects in the architectural styles themselves. The most
significant Event-Based exposure is the combination of a single unbounded worker queue and no durable
recovery: slow work can accumulate, and pending work is discarded during the tested controlled shutdown.
The most significant Layered exposure is cumulative synchronous latency: planning, narration and
reporting all delay the caller in their defined order. The Layered coordinator does, however, already
contain a useful reliability boundary that prevents reporting failure from reversing or falsely failing
an accepted synthetic turn.

At the current scaffold stage, the evidence favors strengthening both implementations rather than
declaring a winner. The Event-Based version needs explicit capacity, durability and recovery semantics.
The Layered version needs latency bounds and removal of secondary reporting work from the response path,
while preserving its clear commit-before-reporting rule. Final architectural judgment should be deferred
until the repositories, AI adapter, gameplay validation and recovery mechanisms are implemented and the
same end-to-end scenarios can be run through both systems.
