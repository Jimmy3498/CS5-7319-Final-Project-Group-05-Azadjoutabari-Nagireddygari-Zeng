"""L1: measure synchronous controlled AI delays through the Layered coordinator.

The experiment uses the real LayeredApplication and LayeredGateway with deterministic
in-memory collaborators. It does not use an external AI API, real persistence, or prove
SQLite durability.
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
from threading import Lock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layered.application import LayeredApplication
from layered.gateway import LayeredGateway
from shared.commands import SubmitTurn
from shared.domain.services import Services
from shared.gateway import OperationStatus
from shared.models.accounts import AccountView
from shared.models.ai import (
    DirectorContext,
    PlayerView,
    ProviderCapture,
    ProviderProfile,
)
from shared.models.common import RequestContext, SecretInput
from shared.models.gameplay import (
    AcceptedTurn,
    CommittedTurnNotice,
    SessionSnapshot,
    TurnAdmission,
    TurnCommit,
    TurnProposal,
)
from shared.models.views import TurnResultView
from shared.models.world import WorldDefinition

EXPERIMENT_ID = "L1"
EXPERIMENT_LABEL = "Layered coordinator simulation using controlled AI-service delays"
AI_DELAYS_SECONDS = (0.0, 0.5, 2.0, 4.0, 6.0)
REPETITIONS = 10
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "L1_raw.csv"
SUMMARY_RESULT_NAME = "L1_summary.json"

RAW_FIELDS = (
    "experiment_id",
    "experiment_label",
    "ai_delay_per_call_seconds",
    "repetition",
    "request_id",
    "operation_id",
    "returned_status",
    "planning_start_ms",
    "planning_finish_ms",
    "planning_duration_ms",
    "narration_start_ms",
    "narration_finish_ms",
    "narration_duration_ms",
    "commit_ms",
    "submit_return_ms",
    "submission_to_commit_ms",
    "total_submit_response_ms",
    "return_after_commit_ms",
    "ai_call_count",
    "commit_count",
    "reporting_call_count",
    "sequence_valid",
    "call_sequence",
)

EMPTY_WORLD = WorldDefinition(
    canon=(),
    characters=(),
    locations=(),
    item_templates=(),
    items=(),
    relationships=(),
    flags=(),
    narrative_rules=(),
    stat_bands=(),
    milestones=(),
)


@dataclass(frozen=True)
class TraceEvent:
    name: str
    timestamp_ns: int


class TraceRecorder:
    def __init__(self) -> None:
        self._events: list[TraceEvent] = []
        self._lock = Lock()

    def record(self, name: str) -> int:
        timestamp_ns = time.perf_counter_ns()
        with self._lock:
            self._events.append(TraceEvent(name, timestamp_ns))
        return timestamp_ns

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def timestamp(self, name: str) -> int | None:
        return next((event.timestamp_ns for event in self.events if event.name == name), None)

    def sequence(self) -> tuple[str, ...]:
        return tuple(event.name for event in self.events)


class FakeAccounts:
    def __init__(self, trace: TraceRecorder) -> None:
        self.trace = trace

    def require_user(self, context: RequestContext) -> AccountView:
        del context
        self.trace.record("authorize")
        return AccountView("synthetic-owner", "synthetic-owner")


class FakeProfiles:
    def __init__(self, trace: TraceRecorder) -> None:
        self.trace = trace
        profile = ProviderProfile(
            id="synthetic-profile",
            owner_id="synthetic-owner",
            name="Controlled delay",
            endpoint="https://example.invalid/v1/chat/completions",
            model="synthetic-model",
            protocol="chat-completions-compatible",
            configuration_revision=1,
            credential_present=True,
        )
        self.capture_value = ProviderCapture(profile, SecretInput("SYNTHETIC-NOT-A-REAL-KEY"))

    def capture(self, owner_id: str) -> ProviderCapture:
        del owner_id
        self.trace.record("capture_profile")
        return self.capture_value


class FakeGameplay:
    def __init__(self, trace: TraceRecorder, baseline: SessionSnapshot) -> None:
        self.trace = trace
        self.baseline = baseline
        self.commit_count = 0

    def prepare(
        self,
        owner_id: str,
        command: SubmitTurn,
        operation_id: str,
        request_id: str,
    ) -> TurnAdmission:
        del owner_id, command, operation_id, request_id
        self.trace.record("prepare")
        return TurnAdmission(self.baseline, None)

    def validate_and_project(
        self, baseline: SessionSnapshot, proposal: TurnProposal
    ) -> SessionSnapshot:
        del proposal
        self.trace.record("validate")
        return baseline

    def history(self, owner_id: str, session_id: str) -> tuple[AcceptedTurn, ...]:
        del owner_id, session_id
        self.trace.record("history")
        return ()

    def commit(
        self,
        owner_id: str,
        operation_id: str,
        command: SubmitTurn,
        candidate: SessionSnapshot,
        narration: str,
    ) -> TurnCommit:
        del owner_id
        self.commit_count += 1
        self.trace.record("commit")
        revision = candidate.revision + 1
        turn = AcceptedTurn(
            id=f"turn-{operation_id}",
            session_id=candidate.id,
            operation_id=operation_id,
            revision=revision,
            player_input=command.player_input,
            narration=narration,
        )
        notice = CommittedTurnNotice(
            event_id=f"notice-{operation_id}",
            operation_id=operation_id,
            session_id=candidate.id,
            campaign_id=candidate.campaign_id,
            base_version_id=candidate.base_version_id,
            revision=revision,
        )
        return TurnCommit(turn, notice)


class FakeKnowledge:
    def __init__(self, trace: TraceRecorder) -> None:
        self.trace = trace

    def director_context(
        self, session: SessionSnapshot, player_input: str, history=()
    ) -> DirectorContext:
        self.trace.record("director_context")
        return DirectorContext(
            campaign_id=session.campaign_id,
            session_id=session.id,
            revision=session.revision,
            world=session.world,
            player_input=player_input,
        )

    def authorize_disclosures(
        self,
        before: SessionSnapshot,
        candidate: SessionSnapshot,
        proposal: TurnProposal,
    ) -> SessionSnapshot:
        del before, proposal
        self.trace.record("authorize_disclosures")
        return candidate

    def player_view(
        self,
        session: SessionSnapshot,
        history: tuple[AcceptedTurn, ...],
        player_input: str,
    ) -> PlayerView:
        del history
        self.trace.record("player_view")
        return PlayerView(
            session_id=session.id,
            revision=session.revision,
            player_character_id=session.player_character_id,
            facts=(),
            accepted_history=(),
            player_input=player_input,
        )


class FakeAI:
    def __init__(self, trace: TraceRecorder, delay_seconds: float) -> None:
        self.trace = trace
        self.delay_seconds = delay_seconds
        self.call_count = 0

    def plan_turn(self, context: DirectorContext, capture: ProviderCapture) -> TurnProposal:
        del capture
        self.call_count += 1
        self.trace.record("plan_start")
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.trace.record("plan_finish")
        return TurnProposal(context.revision, (), ())

    def narrate(self, view: PlayerView, capture: ProviderCapture) -> str:
        del view, capture
        self.call_count += 1
        self.trace.record("narrate_start")
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.trace.record("narrate_finish")
        return "Synthetic narration after controlled delay."


class FakeReporting:
    def __init__(self, trace: TraceRecorder) -> None:
        self.trace = trace
        self.call_count = 0

    def consume_committed_turn(self, notice: CommittedTurnNotice) -> None:
        del notice
        self.call_count += 1
        self.trace.record("report")


class UnusedService:
    """Placeholder for service families not used by SubmitTurn coordination."""


@dataclass(frozen=True)
class TrialResult:
    delay_seconds: float
    repetition: int
    request_id: str
    operation_id: str
    status: str
    submit_started_ns: int
    submit_returned_ns: int
    plan_started_ns: int | None
    plan_finished_ns: int | None
    narration_started_ns: int | None
    narration_finished_ns: int | None
    commit_ns: int | None
    ai_call_count: int
    commit_count: int
    reporting_call_count: int
    call_sequence: tuple[str, ...]
    result_is_turn_view: bool

    @property
    def sequence_valid(self) -> bool:
        values = (
            self.plan_started_ns,
            self.plan_finished_ns,
            self.narration_started_ns,
            self.narration_finished_ns,
            self.commit_ns,
        )
        return (
            all(value is not None for value in values)
            and list(values) == sorted(values)
            and self.commit_ns <= self.submit_returned_ns
        )

    def raw_row(self) -> dict[str, object]:
        return {
            "experiment_id": EXPERIMENT_ID,
            "experiment_label": EXPERIMENT_LABEL,
            "ai_delay_per_call_seconds": self.delay_seconds,
            "repetition": self.repetition,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "returned_status": self.status,
            "planning_start_ms": _relative_ms(self.plan_started_ns, self.submit_started_ns),
            "planning_finish_ms": _relative_ms(self.plan_finished_ns, self.submit_started_ns),
            "planning_duration_ms": _duration_ms(self.plan_started_ns, self.plan_finished_ns),
            "narration_start_ms": _relative_ms(self.narration_started_ns, self.submit_started_ns),
            "narration_finish_ms": _relative_ms(self.narration_finished_ns, self.submit_started_ns),
            "narration_duration_ms": _duration_ms(
                self.narration_started_ns, self.narration_finished_ns
            ),
            "commit_ms": _relative_ms(self.commit_ns, self.submit_started_ns),
            "submit_return_ms": _relative_ms(self.submit_returned_ns, self.submit_started_ns),
            "submission_to_commit_ms": _duration_ms(self.submit_started_ns, self.commit_ns),
            "total_submit_response_ms": _duration_ms(
                self.submit_started_ns, self.submit_returned_ns
            ),
            "return_after_commit_ms": _duration_ms(self.commit_ns, self.submit_returned_ns),
            "ai_call_count": self.ai_call_count,
            "commit_count": self.commit_count,
            "reporting_call_count": self.reporting_call_count,
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
    delay_seconds: float,
) -> tuple[LayeredGateway, TraceRecorder, FakeAI, FakeGameplay, FakeReporting]:
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
    accounts = FakeAccounts(trace)
    profiles = FakeProfiles(trace)
    gameplay = FakeGameplay(trace, baseline)
    knowledge = FakeKnowledge(trace)
    ai = FakeAI(trace, delay_seconds)
    reporting = FakeReporting(trace)
    unused = UnusedService()
    services = Services(
        accounts=accounts,
        world=unused,
        editor=unused,
        knowledge=knowledge,
        gameplay=gameplay,
        profiles=profiles,
        ai=ai,
        updates=unused,
        reporting=reporting,
        queries=unused,
    )
    return LayeredGateway(LayeredApplication(services)), trace, ai, gameplay, reporting


def run_trial(delay_seconds: float, repetition: int) -> TrialResult:
    if delay_seconds < 0:
        raise ValueError("AI delay must be nonnegative")
    if repetition < 1:
        raise ValueError("Repetition numbers start at one")

    gateway, trace, ai, gameplay, reporting = _build_gateway(delay_seconds)
    identity = f"{EXPERIMENT_ID.lower()}-d{delay_seconds}-r{repetition}-{uuid4()}"
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

    return TrialResult(
        delay_seconds=delay_seconds,
        repetition=repetition,
        request_id=request_id,
        operation_id=receipt.operation_id,
        status=receipt.status.value,
        submit_started_ns=submit_started_ns,
        submit_returned_ns=submit_returned_ns,
        plan_started_ns=trace.timestamp("plan_start"),
        plan_finished_ns=trace.timestamp("plan_finish"),
        narration_started_ns=trace.timestamp("narrate_start"),
        narration_finished_ns=trace.timestamp("narrate_finish"),
        commit_ns=trace.timestamp("commit"),
        ai_call_count=ai.call_count,
        commit_count=gameplay.commit_count,
        reporting_call_count=reporting.call_count,
        call_sequence=trace.sequence(),
        result_is_turn_view=isinstance(receipt.result, TurnResultView),
    )


def run_benchmark(
    delays_seconds: tuple[float, ...] = AI_DELAYS_SECONDS,
    repetitions: int = REPETITIONS,
    *,
    show_progress: bool = False,
) -> list[TrialResult]:
    results = []
    total = len(delays_seconds) * repetitions
    completed = 0
    for delay_seconds in delays_seconds:
        for repetition in range(1, repetitions + 1):
            results.append(run_trial(delay_seconds, repetition))
            completed += 1
            if show_progress:
                print(
                    f"L1 progress: {completed}/{total} trials; "
                    f"delay={delay_seconds:.1f}s repetition={repetition}",
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
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "git": _git_metadata(),
        "experiment_script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
    }


def build_summary(
    results: list[TrialResult], metadata: dict[str, object] | None = None
) -> dict[str, object]:
    metadata = environment_metadata() if metadata is None else metadata
    conditions = []
    for delay_seconds in sorted({result.delay_seconds for result in results}):
        matching = [result for result in results if result.delay_seconds == delay_seconds]
        rows = [result.raw_row() for result in matching]
        response = _metric(rows, "total_submit_response_ms")
        planning = _metric(rows, "planning_duration_ms")
        narration = _metric(rows, "narration_duration_ms")
        commit = _metric(rows, "submission_to_commit_ms")
        combined_ai = [plan + narrate for plan, narrate in zip(planning, narration)]
        overhead = [total - ai for total, ai in zip(response, combined_ai)]
        median_response = statistics.median(response)
        median_combined_ai = statistics.median(combined_ai)
        conditions.append(
            {
                "ai_delay_per_call_seconds": delay_seconds,
                "trials": len(matching),
                "completed_receipts": sum(
                    result.status == OperationStatus.COMPLETED.value for result in matching
                ),
                "sequence_valid_trials": sum(result.sequence_valid for result in matching),
                "total_ai_calls": sum(result.ai_call_count for result in matching),
                "total_commits": sum(result.commit_count for result in matching),
                "planning_duration_ms": _statistics(planning),
                "narration_duration_ms": _statistics(narration),
                "combined_ai_duration_ms": _statistics(combined_ai),
                "total_submit_response_ms": _statistics(response),
                "submission_to_commit_ms": _statistics(commit),
                "non_ai_overhead_ms": _statistics(overhead),
                "median_response_attributed_to_ai_percent": round(
                    median_combined_ai / median_response * 100, 6
                )
                if median_response
                else None,
            }
        )

    summary = {
        "experiment_id": EXPERIMENT_ID,
        "label": EXPERIMENT_LABEL,
        "scope": (
            "real synchronous Layered coordinator with deterministic fake services; "
            "no external AI, real persistence, or SQLite durability claim"
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": metadata,
        "configuration": {
            "ai_delays_per_call_seconds": sorted({result.delay_seconds for result in results}),
            "ai_calls_per_trial": 2,
            "repetitions_per_condition": len(
                [result for result in results if result.delay_seconds == results[0].delay_seconds]
            )
            if results
            else 0,
            "coordinator_path": (
                "LayeredGateway.submit -> LayeredApplication.execute -> plan_turn -> narrate "
                "-> commit -> reporting -> return"
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
        lines.append(
            f"delay={condition['ai_delay_per_call_seconds']:.1f}s/call: "
            f"{condition['completed_receipts']}/{condition['trials']} completed; "
            f"response min/median/max={response['minimum']:.3f}/"
            f"{response['median']:.3f}/{response['maximum']:.3f}ms; "
            f"AI contribution={condition['median_response_attributed_to_ai_percent']:.3f}%."
        )
    lines.append(
        "The measurements characterize synchronous coordinator latency with controlled delays; "
        "they do not measure a real provider or prove durable commitment."
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
