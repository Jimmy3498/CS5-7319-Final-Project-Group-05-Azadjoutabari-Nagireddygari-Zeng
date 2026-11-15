"""E2: measure pending synthetic-event handling during controlled bus shutdown.

This experiment uses the production InProcessBus without a background worker. It is a
controlled shutdown with queued messages, not an unexpected process-crash test, and it
does not claim that a successfully committed or processed gameplay turn was lost.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_based.bus import InProcessBus
from event_based.messages import Message, Topic
from shared.models.common import RequestContext

EXPERIMENT_ID = "E2"
EVENT_COUNT = 10
NORMAL_COMPLETION = "normal_completion"
CONTROLLED_SHUTDOWN = "controlled_shutdown_with_pending_messages"
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "E2_raw.csv"
SUMMARY_RESULT_NAME = "E2_summary.json"

RAW_FIELDS = (
    "experiment_id",
    "scenario",
    "event_id",
    "operation_id",
    "work_reference",
    "submitted",
    "successfully_dispatched",
    "discarded_during_shutdown",
    "restored_in_fresh_bus",
    "failure_component",
    "failure_category",
    "final_queue_depth",
    "close_time_ms",
)


@dataclass(frozen=True)
class RecordedFailure:
    event_id: str
    operation_id: str
    component: str
    category: str


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    submitted: tuple[Message, ...]
    processed_event_ids: tuple[str, ...]
    failures: tuple[RecordedFailure, ...]
    restored_event_ids: tuple[str, ...]
    pending_before_action: int
    final_queue_depth: int
    fresh_bus_queue_depth: int | None
    close_time_ns: int

    @property
    def close_time_ms(self) -> float:
        return round(self.close_time_ns / 1_000_000, 6)

    @property
    def discarded_event_ids(self) -> tuple[str, ...]:
        return tuple(
            failure.event_id
            for failure in self.failures
            if failure.category == "queue_closed_before_dispatch"
        )

    def raw_rows(self) -> list[dict[str, object]]:
        processed = set(self.processed_event_ids)
        discarded = set(self.discarded_event_ids)
        restored = set(self.restored_event_ids)
        failures_by_event = {failure.event_id: failure for failure in self.failures}
        rows = []
        for message in self.submitted:
            failure = failures_by_event.get(message.id)
            rows.append(
                {
                    "experiment_id": EXPERIMENT_ID,
                    "scenario": self.scenario,
                    "event_id": message.id,
                    "operation_id": message.operation_id,
                    "work_reference": message.work_reference,
                    "submitted": True,
                    "successfully_dispatched": message.id in processed,
                    "discarded_during_shutdown": message.id in discarded,
                    "restored_in_fresh_bus": message.id in restored,
                    "failure_component": failure.component if failure else "",
                    "failure_category": failure.category if failure else "",
                    "final_queue_depth": self.final_queue_depth,
                    "close_time_ms": self.close_time_ms,
                }
            )
        return rows


def _failure_category(component: str) -> str:
    if component == "shutdown":
        return "queue_closed_before_dispatch"
    if component == "router":
        return "routing_failure"
    return "handler_failure"


def _messages(scenario: str, event_count: int) -> tuple[Message, ...]:
    messages = []
    for sequence in range(1, event_count + 1):
        identity = f"{EXPERIMENT_ID.lower()}-{scenario}-e{sequence:02d}-{uuid4()}"
        context = RequestContext(
            client_session_id=f"client-{identity}",
            user_id=None,
            correlation_id=f"correlation-{identity}",
            request_id=f"request-{identity}",
        )
        messages.append(
            Message(
                id=f"event-{identity}",
                operation_id=f"operation-{identity}",
                correlation_id=context.correlation_id,
                causation_id=None,
                topic=Topic.TURN_CONTEXT,
                context=context,
                work_reference=f"synthetic:{identity}",
            )
        )
    return tuple(messages)


def _new_bus(
    processed_event_ids: list[str],
    failures: list[RecordedFailure],
    collection_lock: Lock,
) -> InProcessBus:
    def on_error(message: Message, component: str, error: Exception) -> None:
        del error  # Exception text is intentionally excluded from experiment output.
        with collection_lock:
            failures.append(
                RecordedFailure(
                    event_id=message.id,
                    operation_id=message.operation_id,
                    component=component,
                    category=_failure_category(component),
                )
            )

    def handler(message: Message) -> None:
        with collection_lock:
            processed_event_ids.append(message.id)

    bus = InProcessBus(on_error)
    bus.subscribe(Topic.TURN_CONTEXT, "e2-synthetic-handler", handler)
    return bus


def run_normal_completion(event_count: int = EVENT_COUNT) -> ScenarioResult:
    if event_count < 1:
        raise ValueError("At least one event is required")
    messages = _messages(NORMAL_COMPLETION, event_count)
    processed_event_ids: list[str] = []
    failures: list[RecordedFailure] = []
    collection_lock = Lock()
    bus = _new_bus(processed_event_ids, failures, collection_lock)

    for message in messages:
        bus.publish(message)
    for _ in messages:
        bus.dispatch_one()

    pending_before_close = bus.pending
    close_started_ns = time.perf_counter_ns()
    bus.close()
    close_time_ns = time.perf_counter_ns() - close_started_ns
    return ScenarioResult(
        scenario=NORMAL_COMPLETION,
        submitted=messages,
        processed_event_ids=tuple(processed_event_ids),
        failures=tuple(failures),
        restored_event_ids=(),
        pending_before_action=pending_before_close,
        final_queue_depth=bus.pending,
        fresh_bus_queue_depth=None,
        close_time_ns=close_time_ns,
    )


def run_controlled_shutdown(event_count: int = EVENT_COUNT) -> ScenarioResult:
    if event_count < 1:
        raise ValueError("At least one event is required")
    messages = _messages(CONTROLLED_SHUTDOWN, event_count)
    processed_event_ids: list[str] = []
    failures: list[RecordedFailure] = []
    collection_lock = Lock()
    bus = _new_bus(processed_event_ids, failures, collection_lock)

    for message in messages:
        bus.publish(message)
    pending_before_close = bus.pending
    close_started_ns = time.perf_counter_ns()
    bus.close()
    close_time_ns = time.perf_counter_ns() - close_started_ns

    fresh_processed: list[str] = []
    fresh_failures: list[RecordedFailure] = []
    fresh_bus = _new_bus(fresh_processed, fresh_failures, Lock())
    fresh_bus_queue_depth = fresh_bus.pending
    restored_event_ids: tuple[str, ...] = ()
    fresh_bus.close()

    return ScenarioResult(
        scenario=CONTROLLED_SHUTDOWN,
        submitted=messages,
        processed_event_ids=tuple(processed_event_ids),
        failures=tuple(failures),
        restored_event_ids=restored_event_ids,
        pending_before_action=pending_before_close,
        final_queue_depth=bus.pending,
        fresh_bus_queue_depth=fresh_bus_queue_depth,
        close_time_ns=close_time_ns,
    )


def run_experiment(event_count: int = EVENT_COUNT) -> list[ScenarioResult]:
    return [run_normal_completion(event_count), run_controlled_shutdown(event_count)]


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


def _scenario_summary(result: ScenarioResult) -> dict[str, object]:
    failure_categories = Counter(failure.category for failure in result.failures)
    return {
        "scenario": result.scenario,
        "events_submitted": len(result.submitted),
        "events_successfully_dispatched": len(result.processed_event_ids),
        "events_discarded_during_shutdown": len(result.discarded_event_ids),
        "events_restored_in_fresh_bus": len(result.restored_event_ids),
        "pending_before_action": result.pending_before_action,
        "final_queue_depth": result.final_queue_depth,
        "fresh_bus_queue_depth": result.fresh_bus_queue_depth,
        "close_time_ms": result.close_time_ms,
        "failure_categories": dict(failure_categories),
    }


def build_summary(
    results: list[ScenarioResult], metadata: dict[str, object] | None = None
) -> dict[str, object]:
    metadata = environment_metadata() if metadata is None else metadata
    scenarios = [_scenario_summary(result) for result in results]
    summary = {
        "experiment_id": EXPERIMENT_ID,
        "scope": (
            "controlled shutdown with queued synthetic messages; not an unexpected crash or "
            "successfully processed gameplay-turn loss test"
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": metadata,
        "configuration": {
            "event_topic": Topic.TURN_CONTEXT.value,
            "events_per_scenario": len(results[0].submitted) if results else 0,
            "background_worker_started": False,
            "recovery_status": "CompletionRecovery.recover is unimplemented and was not invoked",
        },
        "scenarios": scenarios,
        "comparison": scenarios,
        "architectural_scope": (
            "The observed loss is specific to this in-memory queue and close policy. "
            "Event-Based architecture does not inherently require nondurable messaging."
        ),
    }
    summary["human_summary"] = human_summary(summary)
    return summary


def human_summary(summary: dict[str, object]) -> str:
    environment = summary["environment"]
    git = environment["git"]
    lines = [
        "E2 - Pending-Event Loss During Controlled Shutdown",
        summary["scope"].capitalize() + ".",
        f"Python {environment['python_version']}; Git {git['commit_sha']}; "
        f"working tree dirty={git['working_tree_dirty']}.",
    ]
    for scenario in summary["scenarios"]:
        lines.append(
            f"{scenario['scenario']}: submitted={scenario['events_submitted']}, "
            f"dispatched={scenario['events_successfully_dispatched']}, "
            f"discarded={scenario['events_discarded_during_shutdown']}, "
            f"restored={scenario['events_restored_in_fresh_bus']}, "
            f"final depth={scenario['final_queue_depth']}, "
            f"close={scenario['close_time_ms']:.6f}ms."
        )
    lines.append(summary["architectural_scope"])
    return "\n".join(lines)


def write_results(
    results: list[ScenarioResult],
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
