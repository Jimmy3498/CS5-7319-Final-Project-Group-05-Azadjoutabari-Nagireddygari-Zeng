"""E1: measure backlog growth in the real single-worker in-process event bus.

This is a synthetic connector-level experiment. It does not measure end-to-end
gameplay throughput, provider performance, or persistence behavior.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_based.bus import InProcessBus
from event_based.messages import Message, Topic
from shared.models.common import RequestContext

EXPERIMENT_ID = "E1"
EVENT_COUNT = 20
PROCESSING_DELAYS_SECONDS = (0.0, 0.5)
REPETITIONS = 3
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "E1_raw.csv"
SUMMARY_RESULT_NAME = "E1_summary.json"

RAW_FIELDS = (
    "experiment_id",
    "processing_delay_seconds",
    "repetition",
    "sequence",
    "event_id",
    "operation_id",
    "work_reference",
    "publication_completed_ms",
    "handler_start_ms",
    "handler_finish_ms",
    "queue_wait_ms",
    "processing_ms",
    "end_to_end_ms",
    "handler_invocations",
    "completed",
    "incomplete_reason",
)


@dataclass
class EventTiming:
    sequence: int
    event_id: str
    operation_id: str
    work_reference: str
    publication_ready: Event = field(default_factory=Event, repr=False)
    publication_completed_ns: int | None = None
    handler_started_ns: int | None = None
    handler_finished_ns: int | None = None
    handler_invocations: int = 0


@dataclass(frozen=True)
class BusError:
    event_id: str
    operation_id: str
    component: str
    error_type: str


@dataclass
class RunResult:
    experiment_id: str
    processing_delay_seconds: float
    repetition: int
    configured_event_count: int
    timeout_seconds: float
    run_started_ns: int
    publication_finished_ns: int
    observation_finished_ns: int
    maximum_observed_pending_depth: int
    timed_out: bool
    timings: tuple[EventTiming, ...]
    errors: tuple[BusError, ...]

    @property
    def elapsed_ms(self) -> float:
        return _milliseconds(self.observation_finished_ns - self.run_started_ns)

    @property
    def publication_burst_ms(self) -> float:
        return _milliseconds(self.publication_finished_ns - self.run_started_ns)

    def raw_rows(self) -> list[dict[str, object]]:
        errors_by_event: dict[str, list[str]] = {}
        for error in self.errors:
            errors_by_event.setdefault(error.event_id, []).append(
                f"{error.component}:{error.error_type}"
            )

        rows = []
        for timing in sorted(self.timings, key=lambda item: item.sequence):
            completed = (
                timing.handler_invocations == 1
                and timing.handler_finished_ns is not None
                and timing.handler_finished_ns <= self.observation_finished_ns
            )
            reason = ""
            if not completed:
                if timing.handler_invocations > 1:
                    reason = "duplicate_dispatch"
                elif timing.handler_started_ns is None:
                    reason = "timeout_before_dispatch"
                elif timing.handler_finished_ns is None:
                    reason = "timeout_during_processing"
                else:
                    reason = "completed_after_timeout"
            if timing.event_id in errors_by_event:
                error_text = ",".join(errors_by_event[timing.event_id])
                reason = f"{reason};{error_text}" if reason else error_text

            rows.append(
                {
                    "experiment_id": self.experiment_id,
                    "processing_delay_seconds": self.processing_delay_seconds,
                    "repetition": self.repetition,
                    "sequence": timing.sequence,
                    "event_id": timing.event_id,
                    "operation_id": timing.operation_id,
                    "work_reference": timing.work_reference,
                    "publication_completed_ms": _relative_ms(
                        timing.publication_completed_ns, self.run_started_ns
                    ),
                    "handler_start_ms": _relative_ms(
                        timing.handler_started_ns, self.run_started_ns
                    ),
                    "handler_finish_ms": _relative_ms(
                        timing.handler_finished_ns, self.run_started_ns
                    ),
                    "queue_wait_ms": _duration_ms(
                        timing.publication_completed_ns, timing.handler_started_ns
                    ),
                    "processing_ms": _duration_ms(
                        timing.handler_started_ns, timing.handler_finished_ns
                    ),
                    "end_to_end_ms": _duration_ms(
                        timing.publication_completed_ns, timing.handler_finished_ns
                    ),
                    "handler_invocations": timing.handler_invocations,
                    "completed": completed,
                    "incomplete_reason": reason,
                }
            )
        return rows


def _milliseconds(nanoseconds: int) -> float:
    return round(nanoseconds / 1_000_000, 6)


def _relative_ms(value: int | None, origin: int) -> float | None:
    if value is None:
        return None
    return _milliseconds(value - origin)


def _duration_ms(start: int | None, finish: int | None) -> float | None:
    if start is None or finish is None:
        return None
    return _milliseconds(finish - start)


def timeout_for(event_count: int, processing_delay_seconds: float) -> float:
    """Allow twice the expected serial service time plus worker startup margin."""
    return max(5.0, event_count * processing_delay_seconds * 2.0 + 5.0)


def run_backlog_condition(
    processing_delay_seconds: float,
    repetition: int,
    *,
    event_count: int = EVENT_COUNT,
    timeout_seconds: float | None = None,
    experiment_id: str = EXPERIMENT_ID,
) -> RunResult:
    """Run one burst against a fresh real bus and return its observations."""
    if processing_delay_seconds < 0:
        raise ValueError("Processing delay must be nonnegative")
    if repetition < 1:
        raise ValueError("Repetition numbers start at one")
    if event_count < 1:
        raise ValueError("At least one event is required")
    timeout_seconds = (
        timeout_for(event_count, processing_delay_seconds)
        if timeout_seconds is None
        else timeout_seconds
    )
    if timeout_seconds <= 0:
        raise ValueError("Timeout must be positive")

    timings: dict[str, EventTiming] = {}
    messages: list[Message] = []
    for sequence in range(1, event_count + 1):
        identity = f"{experiment_id.lower()}-r{repetition}-e{sequence:02d}-{uuid4()}"
        event_id = f"event-{identity}"
        operation_id = f"operation-{identity}"
        work_reference = f"synthetic:{identity}"
        context = RequestContext(
            client_session_id=f"client-{identity}",
            user_id=None,
            correlation_id=f"correlation-{identity}",
            request_id=f"request-{identity}",
        )
        timings[event_id] = EventTiming(
            sequence=sequence,
            event_id=event_id,
            operation_id=operation_id,
            work_reference=work_reference,
        )
        messages.append(
            Message(
                id=event_id,
                operation_id=operation_id,
                correlation_id=context.correlation_id,
                causation_id=None,
                topic=Topic.TURN_CONTEXT,
                context=context,
                work_reference=work_reference,
            )
        )

    measurements_lock = Lock()
    all_completed = Event()
    errors: list[BusError] = []
    completed_count = 0
    maximum_observed_pending_depth = 0

    def on_error(message: Message, component: str, error: Exception) -> None:
        with measurements_lock:
            errors.append(
                BusError(
                    event_id=message.id,
                    operation_id=message.operation_id,
                    component=component,
                    error_type=type(error).__name__,
                )
            )

    bus = InProcessBus(on_error)

    def observe_pending_depth() -> None:
        nonlocal maximum_observed_pending_depth
        depth = bus.pending
        with measurements_lock:
            maximum_observed_pending_depth = max(maximum_observed_pending_depth, depth)

    def synthetic_handler(message: Message) -> None:
        nonlocal completed_count
        timing = timings[message.id]
        publication_available = timing.publication_ready.wait(timeout=min(timeout_seconds, 5.0))
        if not publication_available:
            on_error(message, "measurement", TimeoutError())

        started_ns = time.perf_counter_ns()
        with measurements_lock:
            timing.handler_invocations += 1
            if timing.handler_started_ns is None:
                timing.handler_started_ns = started_ns
        observe_pending_depth()

        if processing_delay_seconds:
            time.sleep(processing_delay_seconds)

        finished_ns = time.perf_counter_ns()
        with measurements_lock:
            if timing.handler_finished_ns is None:
                timing.handler_finished_ns = finished_ns
                completed_count += 1
            if completed_count == event_count:
                all_completed.set()
        observe_pending_depth()

    bus.subscribe(Topic.TURN_CONTEXT, "e1-synthetic-handler", synthetic_handler)
    bus.start()
    run_started_ns = time.perf_counter_ns()
    for message in messages:
        bus.publish(message)
        publication_completed_ns = time.perf_counter_ns()
        timing = timings[message.id]
        with measurements_lock:
            timing.publication_completed_ns = publication_completed_ns
        timing.publication_ready.set()
        observe_pending_depth()
    publication_finished_ns = time.perf_counter_ns()

    finished_in_time = all_completed.wait(timeout_seconds)
    observation_finished_ns = time.perf_counter_ns()
    observe_pending_depth()
    bus.close()

    return RunResult(
        experiment_id=experiment_id,
        processing_delay_seconds=processing_delay_seconds,
        repetition=repetition,
        configured_event_count=event_count,
        timeout_seconds=timeout_seconds,
        run_started_ns=run_started_ns,
        publication_finished_ns=publication_finished_ns,
        observation_finished_ns=observation_finished_ns,
        maximum_observed_pending_depth=maximum_observed_pending_depth,
        timed_out=not finished_in_time,
        timings=tuple(timings.values()),
        errors=tuple(errors),
    )


def run_experiment(
    *,
    processing_delays_seconds: tuple[float, ...] = PROCESSING_DELAYS_SECONDS,
    repetitions: int = REPETITIONS,
    event_count: int = EVENT_COUNT,
) -> list[RunResult]:
    results = []
    for delay in processing_delays_seconds:
        for repetition in range(1, repetitions + 1):
            results.append(
                run_backlog_condition(
                    delay,
                    repetition,
                    event_count=event_count,
                )
            )
    return results


def _descriptive_statistics(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "mean": None,
            "median": None,
            "p95": None,
            "maximum": None,
        }
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "count": len(values),
        "minimum": round(ordered[0], 6),
        "mean": round(statistics.fmean(values), 6),
        "median": round(statistics.median(values), 6),
        "p95": round(ordered[p95_index], 6),
        "maximum": round(ordered[-1], 6),
    }


def _completed_rows(result: RunResult) -> list[dict[str, object]]:
    return [row for row in result.raw_rows() if row["completed"]]


def _metric(rows: list[dict[str, object]], name: str) -> list[float]:
    return [float(row[name]) for row in rows if row[name] is not None]


def _run_summary(result: RunResult) -> dict[str, object]:
    completed = _completed_rows(result)
    return {
        "processing_delay_seconds": result.processing_delay_seconds,
        "repetition": result.repetition,
        "configured_events": result.configured_event_count,
        "processed_events": len(completed),
        "incomplete_events": result.configured_event_count - len(completed),
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "publication_burst_ms": result.publication_burst_ms,
        "total_elapsed_ms": result.elapsed_ms,
        "throughput_events_per_second": round(len(completed) / (result.elapsed_ms / 1000), 6),
        "maximum_observed_pending_depth": result.maximum_observed_pending_depth,
        "bus_errors": [error.__dict__ for error in result.errors],
        "queue_wait_ms": _descriptive_statistics(_metric(completed, "queue_wait_ms")),
        "processing_ms": _descriptive_statistics(_metric(completed, "processing_ms")),
        "end_to_end_ms": _descriptive_statistics(_metric(completed, "end_to_end_ms")),
    }


def _git_metadata() -> dict[str, object]:
    safe_directory = f"safe.directory={ROOT.as_posix()}"

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", safe_directory, *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        revision = git("rev-parse", "HEAD")
        status = git("status", "--porcelain=v1", "--untracked-files=all")
    except (FileNotFoundError, subprocess.SubprocessError) as error:
        return {
            "commit_sha": "unavailable",
            "working_tree_dirty": None,
            "metadata_error": type(error).__name__,
        }
    if revision.returncode != 0 or status.returncode != 0:
        return {
            "commit_sha": "unavailable",
            "working_tree_dirty": None,
            "metadata_error": "git_command_failed",
        }
    return {
        "commit_sha": revision.stdout.strip(),
        "working_tree_dirty": bool(status.stdout.strip()),
        "metadata_error": None,
    }


def environment_metadata() -> dict[str, object]:
    script_path = Path(__file__).resolve()
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "git": _git_metadata(),
        "experiment_script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
    }


def build_summary(
    results: list[RunResult], metadata: dict[str, object] | None = None
) -> dict[str, object]:
    metadata = environment_metadata() if metadata is None else metadata
    conditions = []
    for delay in sorted({result.processing_delay_seconds for result in results}):
        matching = [result for result in results if result.processing_delay_seconds == delay]
        completed = [row for result in matching for row in _completed_rows(result)]
        total_configured = sum(result.configured_event_count for result in matching)
        conditions.append(
            {
                "processing_delay_seconds": delay,
                "repetitions": len(matching),
                "configured_events": total_configured,
                "processed_events": len(completed),
                "incomplete_events": total_configured - len(completed),
                "maximum_observed_pending_depth": max(
                    result.maximum_observed_pending_depth for result in matching
                ),
                "elapsed_ms_per_repetition": _descriptive_statistics(
                    [result.elapsed_ms for result in matching]
                ),
                "throughput_events_per_second": _descriptive_statistics(
                    [
                        len(_completed_rows(result)) / (result.elapsed_ms / 1000)
                        for result in matching
                    ]
                ),
                "queue_wait_ms": _descriptive_statistics(_metric(completed, "queue_wait_ms")),
                "processing_ms": _descriptive_statistics(_metric(completed, "processing_ms")),
                "end_to_end_ms": _descriptive_statistics(_metric(completed, "end_to_end_ms")),
            }
        )

    summary = {
        "experiment_id": EXPERIMENT_ID,
        "scope": "synthetic connector-level experiment; not end-to-end gameplay throughput",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": metadata,
        "configuration": {
            "event_topic": Topic.TURN_CONTEXT.value,
            "event_count_per_repetition": results[0].configured_event_count if results else 0,
            "processing_delays_seconds": sorted(
                {result.processing_delay_seconds for result in results}
            ),
            "repetitions_per_condition": len(
                [
                    result
                    for result in results
                    if result.processing_delay_seconds == results[0].processing_delay_seconds
                ]
            )
            if results
            else 0,
            "worker_count": 1,
            "clock": "time.perf_counter_ns",
            "queue_depth": "maximum observed Queue.qsize; excludes in-flight work",
            "timeout_policy": "max(5, event_count * delay * 2 + 5) seconds",
        },
        "runs": [_run_summary(result) for result in results],
        "conditions": conditions,
    }
    summary["human_summary"] = human_summary(summary)
    return summary


def human_summary(summary: dict[str, object]) -> str:
    environment = summary["environment"]
    git = environment["git"]
    configuration = summary["configuration"]
    lines = [
        "E1 - Single-Worker Queue Bottleneck",
        "Synthetic connector-level measurements using the production InProcessBus.",
        f"Python {environment['python_version']}; Git {git['commit_sha']}; "
        f"working tree dirty={git['working_tree_dirty']}.",
        f"Configuration: {configuration['event_count_per_repetition']} events/repetition, "
        f"{configuration['repetitions_per_condition']} repetitions/condition, "
        f"delays={configuration['processing_delays_seconds']}, "
        f"workers={configuration['worker_count']}.",
    ]
    for condition in summary["conditions"]:
        delay = condition["processing_delay_seconds"]
        elapsed = condition["elapsed_ms_per_repetition"]["mean"]
        wait = condition["queue_wait_ms"]["p95"]
        throughput = condition["throughput_events_per_second"]["mean"]
        lines.append(
            f"delay={delay:.3f}s: {condition['processed_events']}/"
            f"{condition['configured_events']} completed; mean elapsed={elapsed:.3f}ms; "
            f"p95 queue wait={wait:.3f}ms; mean throughput={throughput:.3f} events/s; "
            f"max observed pending={condition['maximum_observed_pending_depth']}."
        )
    lines.append(
        "Interpretation is limited to the current in-memory, single-worker event connector."
    )
    return "\n".join(lines)


def write_results(
    results: list[RunResult],
    output_directory: Path = RESULTS_DIRECTORY,
    metadata: dict[str, object] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    output_directory.mkdir(parents=True, exist_ok=True)
    raw_path = output_directory / RAW_RESULT_NAME
    summary_path = output_directory / SUMMARY_RESULT_NAME

    rows = [row for result in results for row in result.raw_rows()]
    with raw_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    summary = build_summary(results, metadata)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return raw_path, summary_path, summary


def main() -> None:
    metadata = environment_metadata()
    results = run_experiment()
    raw_path, summary_path, summary = write_results(results, metadata=metadata)
    print(summary["human_summary"])
    print(f"Raw results: {raw_path.relative_to(ROOT)}")
    print(f"Statistics: {summary_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
