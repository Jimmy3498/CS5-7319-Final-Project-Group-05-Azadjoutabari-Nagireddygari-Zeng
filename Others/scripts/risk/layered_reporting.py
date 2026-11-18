"""L2: measure synchronous reporting delay and post-commit failure isolation.

This is a Layered coordinator/reporting simulation using the real LayeredApplication
and LayeredGateway with deterministic collaborators. The synthetic committed-state
record is not durable SQLite persistence, and reporting recovery is not evaluated.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layered.application import LayeredApplication
from layered.gateway import LayeredGateway
from scripts.risk.layered_latency import (
    EMPTY_WORLD,
    FakeAccounts,
    FakeAI,
    FakeGameplay,
    FakeKnowledge,
    FakeProfiles,
    TraceRecorder,
    UnusedService,
)
from shared.commands import SubmitTurn
from shared.domain.services import Services
from shared.gateway import OperationStatus
from shared.models.common import RequestContext
from shared.models.gameplay import CommittedTurnNotice, SessionSnapshot, TurnCommit
from shared.models.views import TurnResultView

EXPERIMENT_ID = "L2"
EXPERIMENT_LABEL = "Layered coordinator/reporting simulation"
REPETITIONS = 10
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "L2_raw.csv"
SUMMARY_RESULT_NAME = "L2_summary.json"

RAW_FIELDS = (
    "experiment_id",
    "experiment_label",
    "scenario",
    "reporting_delay_seconds",
    "reporting_failure_configured",
    "repetition",
    "request_id",
    "operation_id",
    "returned_status",
    "reporting_pending",
    "submission_to_commit_ms",
    "reporting_start_ms",
    "reporting_finish_ms",
    "reporting_duration_ms",
    "total_submit_response_ms",
    "return_after_reporting_ms",
    "commit_count",
    "committed_state_record_count",
    "reporting_success_count",
    "reporting_failure_count",
    "commit_before_reporting",
    "commit_exactly_once",
    "committed_state_preserved",
    "sequence_valid",
    "call_sequence",
)


@dataclass(frozen=True)
class ScenarioConfig:
    id: str
    reporting_delay_seconds: float
    reporting_failure: bool


SCENARIOS = (
    ScenarioConfig("A_normal_reporting", 0.0, False),
    ScenarioConfig("B_reporting_delay_0_5s", 0.5, False),
    ScenarioConfig("C_reporting_delay_2s", 2.0, False),
    ScenarioConfig("D_reporting_failure_after_commit", 0.0, True),
)


class ControlledReportingFailure(RuntimeError):
    """Synthetic failure type; its message is not persisted in experiment outputs."""


class RecordingGameplay(FakeGameplay):
    def __init__(self, trace: TraceRecorder, baseline: SessionSnapshot) -> None:
        super().__init__(trace, baseline)
        self.committed_state: dict[str, TurnCommit] = {}

    def commit(
        self,
        owner_id: str,
        operation_id: str,
        command: SubmitTurn,
        candidate: SessionSnapshot,
        narration: str,
    ) -> TurnCommit:
        result = super().commit(owner_id, operation_id, command, candidate, narration)
        self.committed_state[operation_id] = result
        return result


class ConfigurableReporting:
    def __init__(
        self,
        trace: TraceRecorder,
        delay_seconds: float,
        fail_after_commit: bool,
    ) -> None:
        self.trace = trace
        self.delay_seconds = delay_seconds
        self.fail_after_commit = fail_after_commit
        self.success_count = 0
        self.failure_count = 0

    def consume_committed_turn(self, notice: CommittedTurnNotice) -> None:
        del notice
        self.trace.record("report_start")
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail_after_commit:
            self.failure_count += 1
            self.trace.record("report_failure")
            raise ControlledReportingFailure()
        self.success_count += 1
        self.trace.record("report_finish")


@dataclass(frozen=True)
class TrialResult:
    scenario: ScenarioConfig
    repetition: int
    request_id: str
    operation_id: str
    status: str
    reporting_pending: bool | None
    submit_started_ns: int
    submit_returned_ns: int
    commit_ns: int | None
    reporting_started_ns: int | None
    reporting_finished_ns: int | None
    commit_count: int
    committed_state_record_count: int
    reporting_success_count: int
    reporting_failure_count: int
    call_sequence: tuple[str, ...]
    result_is_turn_view: bool

    @property
    def commit_before_reporting(self) -> bool:
        return (
            self.commit_ns is not None
            and self.reporting_started_ns is not None
            and self.commit_ns <= self.reporting_started_ns
        )

    @property
    def commit_exactly_once(self) -> bool:
        return self.commit_count == 1 and self.committed_state_record_count == 1

    @property
    def committed_state_preserved(self) -> bool:
        return self.committed_state_record_count == 1

    @property
    def sequence_valid(self) -> bool:
        return (
            self.commit_before_reporting
            and self.reporting_finished_ns is not None
            and self.reporting_started_ns <= self.reporting_finished_ns
            and self.reporting_finished_ns <= self.submit_returned_ns
        )

    def raw_row(self) -> dict[str, object]:
        return {
            "experiment_id": EXPERIMENT_ID,
            "experiment_label": EXPERIMENT_LABEL,
            "scenario": self.scenario.id,
            "reporting_delay_seconds": self.scenario.reporting_delay_seconds,
            "reporting_failure_configured": self.scenario.reporting_failure,
            "repetition": self.repetition,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "returned_status": self.status,
            "reporting_pending": self.reporting_pending,
            "submission_to_commit_ms": _duration_ms(self.submit_started_ns, self.commit_ns),
            "reporting_start_ms": _relative_ms(self.reporting_started_ns, self.submit_started_ns),
            "reporting_finish_ms": _relative_ms(self.reporting_finished_ns, self.submit_started_ns),
            "reporting_duration_ms": _duration_ms(
                self.reporting_started_ns, self.reporting_finished_ns
            ),
            "total_submit_response_ms": _duration_ms(
                self.submit_started_ns, self.submit_returned_ns
            ),
            "return_after_reporting_ms": _duration_ms(
                self.reporting_finished_ns, self.submit_returned_ns
            ),
            "commit_count": self.commit_count,
            "committed_state_record_count": self.committed_state_record_count,
            "reporting_success_count": self.reporting_success_count,
            "reporting_failure_count": self.reporting_failure_count,
            "commit_before_reporting": self.commit_before_reporting,
            "commit_exactly_once": self.commit_exactly_once,
            "committed_state_preserved": self.committed_state_preserved,
            "sequence_valid": self.sequence_valid,
            "call_sequence": ">".join(self.call_sequence),
        }


def _relative_ms(value: int | None, origin: int) -> float | None:
    if value is None:
        return None
    return round((value - origin) / 1_000_000, 6)


def _duration_ms(start: int | None, finish: int | None) -> float | None:
    if start is None or finish is None:
        return None
    return round((finish - start) / 1_000_000, 6)


def _build_gateway(
    scenario: ScenarioConfig,
) -> tuple[LayeredGateway, TraceRecorder, RecordingGameplay, ConfigurableReporting]:
    trace = TraceRecorder()
    baseline = SessionSnapshot(
        id="synthetic-session",
        owner_id="synthetic-owner",
        campaign_id="synthetic-campaign",
        base_version_id="synthetic-version",
        revision=0,
        player_character_id="synthetic-player",
        world=EMPTY_WORLD,
        disclosed_ids=(),
        flag_values=(),
    )
    gameplay = RecordingGameplay(trace, baseline)
    reporting = ConfigurableReporting(
        trace,
        scenario.reporting_delay_seconds,
        scenario.reporting_failure,
    )
    unused = UnusedService()
    services = Services(
        accounts=FakeAccounts(trace),
        world=unused,
        editor=unused,
        knowledge=FakeKnowledge(trace),
        gameplay=gameplay,
        profiles=FakeProfiles(trace),
        ai=FakeAI(trace, 0.0),
        updates=unused,
        reporting=reporting,
        queries=unused,
    )
    return LayeredGateway(LayeredApplication(services)), trace, gameplay, reporting


def run_trial(scenario: ScenarioConfig, repetition: int) -> TrialResult:
    if repetition < 1:
        raise ValueError("Repetition numbers start at one")
    gateway, trace, gameplay, reporting = _build_gateway(scenario)
    identity = f"{EXPERIMENT_ID.lower()}-{scenario.id}-r{repetition}-{uuid4()}"
    request_id = f"request-{identity}"
    context = RequestContext(
        client_session_id=f"client-{identity}",
        user_id="synthetic-owner",
        correlation_id=f"correlation-{identity}",
    )
    command = SubmitTurn("synthetic-session", 0, "Take a synthetic action")
    submit_started_ns = time.perf_counter_ns()
    receipt = gateway.submit(context, command, request_id)
    submit_returned_ns = time.perf_counter_ns()
    gateway.close()

    result = receipt.result if isinstance(receipt.result, TurnResultView) else None
    report_terminal_name = "report_failure" if scenario.reporting_failure else "report_finish"
    return TrialResult(
        scenario=scenario,
        repetition=repetition,
        request_id=request_id,
        operation_id=receipt.operation_id,
        status=receipt.status.value,
        reporting_pending=result.reporting_pending if result else None,
        submit_started_ns=submit_started_ns,
        submit_returned_ns=submit_returned_ns,
        commit_ns=trace.timestamp("commit"),
        reporting_started_ns=trace.timestamp("report_start"),
        reporting_finished_ns=trace.timestamp(report_terminal_name),
        commit_count=gameplay.commit_count,
        committed_state_record_count=len(gameplay.committed_state),
        reporting_success_count=reporting.success_count,
        reporting_failure_count=reporting.failure_count,
        call_sequence=trace.sequence(),
        result_is_turn_view=result is not None,
    )


def run_benchmark(
    scenarios: tuple[ScenarioConfig, ...] = SCENARIOS,
    repetitions: int = REPETITIONS,
    *,
    show_progress: bool = False,
) -> list[TrialResult]:
    results = []
    total = len(scenarios) * repetitions
    completed = 0
    for scenario in scenarios:
        for repetition in range(1, repetitions + 1):
            results.append(run_trial(scenario, repetition))
            completed += 1
            if show_progress:
                print(
                    f"L2 progress: {completed}/{total} trials; "
                    f"scenario={scenario.id} repetition={repetition}",
                    flush=True,
                )
    return results


def _statistics(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "minimum": None, "median": None, "maximum": None}
    return {
        "count": len(values),
        "minimum": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "maximum": round(max(values), 6),
    }


def _metric(rows: list[dict[str, object]], name: str) -> list[float]:
    return [float(row[name]) for row in rows if row[name] is not None]


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
    fixture_path = ROOT / "scripts" / "risk" / "layered_latency.py"
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "git": _git_metadata(),
        "experiment_script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "shared_l1_fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    }


def build_summary(
    results: list[TrialResult], metadata: dict[str, object] | None = None
) -> dict[str, object]:
    metadata = environment_metadata() if metadata is None else metadata
    scenarios_by_id: dict[str, ScenarioConfig] = {}
    for result in results:
        scenarios_by_id.setdefault(result.scenario.id, result.scenario)
    grouped: list[tuple[ScenarioConfig, list[TrialResult]]] = []
    for scenario in scenarios_by_id.values():
        matching = [result for result in results if result.scenario.id == scenario.id]
        if matching:
            grouped.append((scenario, matching))

    baseline_scenario, baseline_values = next(
        (
            (scenario, values)
            for scenario, values in grouped
            if scenario.reporting_delay_seconds == 0 and not scenario.reporting_failure
        ),
        grouped[0],
    )
    del baseline_scenario
    baseline_rows = [result.raw_row() for result in baseline_values]
    baseline_response = statistics.median(_metric(baseline_rows, "total_submit_response_ms"))
    conditions = []
    for scenario, matching in grouped:
        rows = [result.raw_row() for result in matching]
        response = _metric(rows, "total_submit_response_ms")
        median_response = statistics.median(response)
        conditions.append(
            {
                "scenario": scenario.id,
                "reporting_delay_seconds": scenario.reporting_delay_seconds,
                "reporting_failure_configured": scenario.reporting_failure,
                "trials": len(matching),
                "completed_receipts": sum(
                    result.status == OperationStatus.COMPLETED.value for result in matching
                ),
                "reporting_pending_true": sum(
                    result.reporting_pending is True for result in matching
                ),
                "successful_simulated_commits": sum(
                    result.committed_state_preserved for result in matching
                ),
                "commit_exactly_once_trials": sum(
                    result.commit_exactly_once for result in matching
                ),
                "reporting_successes": sum(result.reporting_success_count for result in matching),
                "reporting_failures": sum(result.reporting_failure_count for result in matching),
                "commit_before_reporting_trials": sum(
                    result.commit_before_reporting for result in matching
                ),
                "sequence_valid_trials": sum(result.sequence_valid for result in matching),
                "submission_to_commit_ms": _statistics(_metric(rows, "submission_to_commit_ms")),
                "reporting_duration_ms": _statistics(_metric(rows, "reporting_duration_ms")),
                "total_submit_response_ms": _statistics(response),
                "median_response_increase_vs_normal_ms": round(
                    median_response - baseline_response, 6
                ),
            }
        )

    summary = {
        "experiment_id": EXPERIMENT_ID,
        "label": EXPERIMENT_LABEL,
        "scope": (
            "real Layered coordinator with deterministic gameplay/reporting doubles; "
            "not a complete integration test or durable SQLite persistence proof"
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": metadata,
        "configuration": {
            "repetitions_per_scenario": max((len(values) for _, values in grouped), default=0),
            "scenarios": [scenario.__dict__ for scenario, _ in grouped],
            "catch_up_status": (
                "ReportingService.catch_up is implemented; recovery is outside this experiment "
                "and was not evaluated"
            ),
        },
        "conditions": conditions,
    }
    summary["human_summary"] = human_summary(summary)
    return summary


def human_summary(summary: dict[str, object]) -> str:
    environment = summary["environment"]
    git = environment["git"]
    lines = [
        f"{EXPERIMENT_ID} - {EXPERIMENT_LABEL}",
        f"Python {environment['python_version']}; Git {git['commit_sha']}; "
        f"working tree dirty={git['working_tree_dirty']}.",
    ]
    for condition in summary["conditions"]:
        response = condition["total_submit_response_ms"]
        reporting = condition["reporting_duration_ms"]
        lines.append(
            f"{condition['scenario']}: completed={condition['completed_receipts']}/"
            f"{condition['trials']}; response median={response['median']:.3f}ms; "
            f"reporting median={reporting['median']:.3f}ms; "
            f"report failures={condition['reporting_failures']}; "
            f"reporting_pending={condition['reporting_pending_true']}."
        )
    lines.append(
        "ReportingService.catch_up is implemented, but reporting recovery was outside this "
        "experiment and was not evaluated."
    )
    return "\n".join(lines)


def write_results(
    results: list[TrialResult],
    output_directory: Path = RESULTS_DIRECTORY,
    metadata: dict[str, object] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    output_directory.mkdir(parents=True, exist_ok=True)
    raw_path = output_directory / RAW_RESULT_NAME
    summary_path = output_directory / SUMMARY_RESULT_NAME

    rows = [result.raw_row() for result in results]
    with raw_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    summary = build_summary(results, metadata)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return raw_path, summary_path, summary


def main() -> None:
    metadata = environment_metadata()
    results = run_benchmark(show_progress=True)
    raw_path, summary_path, summary = write_results(results, metadata=metadata)
    print(summary["human_summary"], flush=True)
    print(f"Raw results: {raw_path.relative_to(ROOT)}", flush=True)
    print(f"Statistics: {summary_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
