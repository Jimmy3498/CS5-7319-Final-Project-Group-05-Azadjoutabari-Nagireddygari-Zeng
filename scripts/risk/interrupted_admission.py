"""R2: verify startup classification and explicit retry of interrupted admissions."""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.risk.scenario_support import (
    MemoryVault,
    RecordingProvider,
    build_application,
    seed_scenario,
    submit_and_drain,
)
from shared import commands as c
from shared.errors import ErrorCode
from shared.queries import Query, QueryKind

EXPERIMENT_ID = "R2"
ARCHITECTURES = ("Layered", "Event-Based")
REPETITIONS = 3
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "R2_raw.csv"
SUMMARY_RESULT_NAME = "R2_summary.json"
RAW_FIELDS = (
    "experiment_id",
    "architecture",
    "repetition",
    "receipt_before_restart",
    "attempt_before_restart",
    "receipt_after_restart",
    "attempt_after_restart",
    "public_error_after_restart",
    "uncertain_after_restart",
    "pending_notices_after_restart",
    "provider_calls_before_retry",
    "retry_receipt_status",
    "retry_linked_to_interrupted_attempt",
    "interrupted_version_matches_session",
    "retry_version_matches_session",
    "provider_calls_after_retry",
    "state_revision_after_retry",
    "accepted_turns_after_retry",
    "version_report_accepted_turns",
    "second_restart_stable",
    "recovery_ms",
    "recovery_passed",
)


@dataclass(frozen=True)
class AdmissionRecoveryResult:
    architecture: str
    repetition: int
    receipt_before_restart: str
    attempt_before_restart: str
    receipt_after_restart: str
    attempt_after_restart: str
    public_error_after_restart: str | None
    uncertain_after_restart: bool
    pending_notices_after_restart: int
    provider_calls_before_retry: int
    retry_receipt_status: str
    retry_linked_to_interrupted_attempt: bool
    interrupted_version_matches_session: bool
    retry_version_matches_session: bool
    provider_calls_after_retry: int
    state_revision_after_retry: int
    accepted_turns_after_retry: int
    version_report_accepted_turns: int
    second_restart_stable: bool
    recovery_ms: float

    @property
    def recovery_passed(self) -> bool:
        return (
            self.receipt_before_restart == "running"
            and self.attempt_before_restart == "running"
            and self.receipt_after_restart == "failed"
            and self.attempt_after_restart == "failed"
            and self.public_error_after_restart == ErrorCode.PROVIDER_UNCERTAIN.value
            and self.uncertain_after_restart
            and self.pending_notices_after_restart == 0
            and self.provider_calls_before_retry == 0
            and self.retry_receipt_status == "completed"
            and self.retry_linked_to_interrupted_attempt
            and self.interrupted_version_matches_session
            and self.retry_version_matches_session
            and self.provider_calls_after_retry == 2
            and self.state_revision_after_retry == 1
            and self.accepted_turns_after_retry == 1
            and self.version_report_accepted_turns == 1
            and self.second_restart_stable
        )

    def raw_row(self) -> dict[str, object]:
        return {"experiment_id": EXPERIMENT_ID, **asdict(self), "recovery_passed": self.recovery_passed}


def _attempt_row(store, operation_id: str) -> dict[str, object]:
    with closing(store._connect()) as database:
        row = database.execute(
            """
            SELECT r.status AS receipt_status, r.public_error_code,
                   t.id AS attempt_id, t.prior_attempt_id, t.base_version_id,
                   t.state AS attempt_state, t.provider_outcome_uncertain
            FROM operation_receipts AS r
            JOIN turn_attempts AS t ON t.operation_id = r.operation_id
            WHERE r.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Risk attempt was not persisted")
    return dict(row)


def run_trial(architecture: str, repetition: int) -> AdmissionRecoveryResult:
    if architecture not in ARCHITECTURES or repetition < 1:
        raise ValueError("Invalid R2 trial configuration")
    with TemporaryDirectory(prefix="r2-interrupted-admission-") as temporary:
        root = Path(temporary)
        provider, vault = RecordingProvider(), MemoryVault()
        app = build_application(architecture, root, provider, vault)
        scenario = seed_scenario(app, architecture, root)
        interrupted_id = f"r2-interrupted-{repetition}"
        scenario.store.admit_turn(
            scenario.owner_id,
            interrupted_id,
            f"r2-admission-{repetition}",
            scenario.session_id,
            0,
            "Wait and observe",
        )
        before = _attempt_row(scenario.store, interrupted_id)
        app.close()

        recovery_started = time.perf_counter_ns()
        recovered = build_application(architecture, root, provider, vault)
        recovery_ms = round((time.perf_counter_ns() - recovery_started) / 1_000_000, 6)
        try:
            after = _attempt_row(scenario.store, interrupted_id)
            pending_after = len(scenario.store.pending_notifications())
            calls_before_retry = len(provider.calls)
            retry = submit_and_drain(
                recovered,
                architecture,
                scenario.context,
                c.RetryTurn(interrupted_id, scenario.session_id, 0),
                f"r2-retry-{repetition}",
            )
            retry_row = _attempt_row(scenario.store, retry.operation_id)
            state = scenario.store.load_session(scenario.owner_id, scenario.session_id)
            history = scenario.store.accepted_history(scenario.owner_id, scenario.session_id)
            report = recovered.query(
                scenario.context,
                Query(QueryKind.CAMPAIGN_REPORT, scenario.campaign_id, scenario.version_id),
            )
            snapshot = (
                after,
                retry_row,
                state,
                history,
                len(provider.calls),
                len(scenario.store.pending_notifications()),
            )
        finally:
            recovered.close()

        restarted = build_application(architecture, root, provider, vault)
        try:
            if architecture == "Event-Based":
                while restarted.bus.pending:
                    if not restarted.bus.dispatch_one():
                        raise RuntimeError("R2 second restart stopped before catch-up")
            stable = snapshot == (
                _attempt_row(scenario.store, interrupted_id),
                _attempt_row(scenario.store, retry.operation_id),
                scenario.store.load_session(scenario.owner_id, scenario.session_id),
                scenario.store.accepted_history(scenario.owner_id, scenario.session_id),
                len(provider.calls),
                len(scenario.store.pending_notifications()),
            )
        finally:
            restarted.close()

        return AdmissionRecoveryResult(
            architecture=architecture,
            repetition=repetition,
            receipt_before_restart=str(before["receipt_status"]),
            attempt_before_restart=str(before["attempt_state"]),
            receipt_after_restart=str(after["receipt_status"]),
            attempt_after_restart=str(after["attempt_state"]),
            public_error_after_restart=after["public_error_code"],
            uncertain_after_restart=bool(after["provider_outcome_uncertain"]),
            pending_notices_after_restart=pending_after,
            provider_calls_before_retry=calls_before_retry,
            retry_receipt_status=retry.status.value,
            retry_linked_to_interrupted_attempt=retry_row["prior_attempt_id"] == after["attempt_id"],
            interrupted_version_matches_session=after["base_version_id"] == scenario.version_id,
            retry_version_matches_session=retry_row["base_version_id"] == scenario.version_id,
            provider_calls_after_retry=len(provider.calls),
            state_revision_after_retry=state.revision,
            accepted_turns_after_retry=len(history),
            version_report_accepted_turns=report.accepted_turns,
            second_restart_stable=stable,
            recovery_ms=recovery_ms,
        )


def run_experiment(repetitions: int = REPETITIONS) -> list[AdmissionRecoveryResult]:
    return [
        run_trial(architecture, repetition)
        for architecture in ARCHITECTURES
        for repetition in range(1, repetitions + 1)
    ]


def _git_metadata() -> dict[str, object]:
    safe = f"safe.directory={ROOT.as_posix()}"
    revision = subprocess.run(
        ["git", "-c", safe, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    status = subprocess.run(
        ["git", "-c", safe, "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return {
        "commit_sha": revision.stdout.strip() if revision.returncode == 0 else "unavailable",
        "working_tree_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def environment_metadata() -> dict[str, object]:
    support = ROOT / "scripts" / "risk" / "scenario_support.py"
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "git": _git_metadata(),
        "experiment_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "support_script_sha256": hashlib.sha256(support.read_bytes()).hexdigest(),
    }


def build_summary(results: list[AdmissionRecoveryResult], metadata=None) -> dict[str, object]:
    conditions = []
    for architecture in ARCHITECTURES:
        matching = [result for result in results if result.architecture == architecture]
        recovery_values = [result.recovery_ms for result in matching]
        conditions.append(
            {
                "architecture": architecture,
                "trials": len(matching),
                "passed": sum(result.recovery_passed for result in matching),
                "provider_calls_before_retry": sum(
                    result.provider_calls_before_retry for result in matching
                ),
                "provider_calls_after_retry": sum(
                    result.provider_calls_after_retry for result in matching
                ),
                "version_attribution_passed": sum(
                    result.interrupted_version_matches_session
                    and result.retry_version_matches_session
                    and result.version_report_accepted_turns == 1
                    for result in matching
                ),
                "recovery_ms": {
                    "minimum": round(min(recovery_values), 6) if recovery_values else None,
                    "median": round(statistics.median(recovery_values), 6)
                    if recovery_values
                    else None,
                    "maximum": round(max(recovery_values), 6) if recovery_values else None,
                },
            }
        )
    return {
        "experiment_id": EXPERIMENT_ID,
        "scope": "admitted pre-commit interruption, startup classification and explicit retry",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment_metadata() if metadata is None else metadata,
        "configuration": {
            "architectures": list(ARCHITECTURES),
            "repetitions_per_architecture": REPETITIONS,
            "startup_sequence": (
                "initialize schema; fail abandoned running attempts; recover committed consumers"
            ),
            "provider": "deterministic recording provider; called only after explicit retry",
        },
        "conditions": conditions,
        "all_trials_passed": all(result.recovery_passed for result in results),
    }


def write_results(results, output_directory=RESULTS_DIRECTORY, metadata=None):
    output_directory.mkdir(parents=True, exist_ok=True)
    raw_path, summary_path = output_directory / RAW_RESULT_NAME, output_directory / SUMMARY_RESULT_NAME
    with raw_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(result.raw_row() for result in results)
    summary = build_summary(results, metadata)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return raw_path, summary_path, summary


def main() -> None:
    metadata = environment_metadata()
    results = run_experiment()
    raw_path, summary_path, summary = write_results(results, metadata=metadata)
    for condition in summary["conditions"]:
        print(
            f"R2 {condition['architecture']}: {condition['passed']}/{condition['trials']} passed; "
            f"recovery median={condition['recovery_ms']['median']:.3f}ms"
        )
    print(f"Raw results: {raw_path.relative_to(ROOT)}")
    print(f"Statistics: {summary_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
