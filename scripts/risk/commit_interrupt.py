"""R1: interrupt after durable commit and verify restart catch-up without replay."""

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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.risk.scenario_support import (
    MemoryVault,
    RecordingProvider,
    build_application,
    seed_scenario,
)
from shared.queries import Query, QueryKind

EXPERIMENT_ID = "R1"
ARCHITECTURES = ("Layered", "Event-Based")
REPETITIONS = 3
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "R1_raw.csv"
SUMMARY_RESULT_NAME = "R1_summary.json"
RAW_FIELDS = (
    "experiment_id",
    "architecture",
    "repetition",
    "status_at_interruption",
    "pending_notices_before_restart",
    "consumer_receipts_before_restart",
    "pending_notices_after_restart",
    "consumer_receipts_after_restart",
    "state_revision_after_restart",
    "accepted_turns_after_restart",
    "provider_calls_before_restart",
    "provider_calls_after_restart",
    "recovery_ms",
    "recovery_passed",
)


@dataclass(frozen=True)
class RecoveryResult:
    architecture: str
    repetition: int
    status_at_interruption: str
    pending_notices_before_restart: int
    consumer_receipts_before_restart: int
    pending_notices_after_restart: int
    consumer_receipts_after_restart: int
    state_revision_after_restart: int
    accepted_turns_after_restart: int
    provider_calls_before_restart: int
    provider_calls_after_restart: int
    recovery_ms: float

    @property
    def recovery_passed(self) -> bool:
        return (
            self.pending_notices_before_restart == 1
            and self.consumer_receipts_before_restart == 0
            and self.pending_notices_after_restart == 0
            and self.consumer_receipts_after_restart == 2
            and self.state_revision_after_restart == 1
            and self.accepted_turns_after_restart == 1
            and self.provider_calls_before_restart == 2
            and self.provider_calls_after_restart == 2
        )

    def raw_row(self) -> dict[str, object]:
        return {"experiment_id": EXPERIMENT_ID, **asdict(self), "recovery_passed": self.recovery_passed}


def _consumer_receipt_count(store) -> int:
    with closing(store._connect()) as database:
        return database.execute("SELECT COUNT(*) FROM consumer_receipts").fetchone()[0]


def _interrupt_after_commit(app, scenario, architecture: str, request_id: str) -> str:
    if architecture == "Layered":
        with patch.object(
            app.application.services.gameplay,
            "acknowledge",
            side_effect=RuntimeError("controlled interruption after commit"),
        ):
            receipt = app.submit(scenario.context, scenario.turn_command, request_id)
        return receipt.status.value

    receipt = app.submit(scenario.context, scenario.turn_command, request_id)
    for _ in range(10):
        if scenario.store.pending_notifications():
            break
        if not app.bus.dispatch_one():
            raise RuntimeError("Event pipeline stopped before commit")
    if not scenario.store.pending_notifications():
        raise RuntimeError("Event pipeline did not reach the durable commit boundary")
    return app.poll(scenario.context, receipt.operation_id).status.value


def run_trial(architecture: str, repetition: int) -> RecoveryResult:
    if architecture not in ARCHITECTURES or repetition < 1:
        raise ValueError("Invalid R1 trial configuration")
    with TemporaryDirectory(prefix="r1-commit-interrupt-") as temporary:
        root = Path(temporary)
        provider, vault = RecordingProvider(), MemoryVault()
        app = build_application(architecture, root, provider, vault)
        scenario = seed_scenario(app, architecture, root)
        status = _interrupt_after_commit(app, scenario, architecture, f"r1-turn-{repetition}")
        pending_before = len(scenario.store.pending_notifications())
        receipts_before = _consumer_receipt_count(scenario.store)
        calls_before = len(provider.calls)
        app.close()

        recovery_started = time.perf_counter_ns()
        recovered = build_application(architecture, root, provider, vault)
        try:
            if architecture == "Event-Based":
                while recovered.bus.pending:
                    if not recovered.bus.dispatch_one():
                        raise RuntimeError("Recovered event bus stopped before catch-up")
            recovery_ms = round((time.perf_counter_ns() - recovery_started) / 1_000_000, 6)
            pending_after = len(scenario.store.pending_notifications())
            receipts_after = _consumer_receipt_count(scenario.store)
            state = scenario.store.load_session(scenario.owner_id, scenario.session_id)
            history = scenario.store.accepted_history(scenario.owner_id, scenario.session_id)
            report = recovered.query(
                scenario.context,
                Query(QueryKind.CAMPAIGN_REPORT, scenario.campaign_id, scenario.version_id),
            )
            if report.accepted_turns != 1:
                raise RuntimeError("Recovered report did not contain exactly one accepted turn")
            return RecoveryResult(
                architecture,
                repetition,
                status,
                pending_before,
                receipts_before,
                pending_after,
                receipts_after,
                state.revision,
                len(history),
                calls_before,
                len(provider.calls),
                recovery_ms,
            )
        finally:
            recovered.close()


def run_experiment(repetitions: int = REPETITIONS) -> list[RecoveryResult]:
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


def build_summary(results: list[RecoveryResult], metadata=None) -> dict[str, object]:
    conditions = []
    for architecture in ARCHITECTURES:
        matching = [result for result in results if result.architecture == architecture]
        recovery_values = [result.recovery_ms for result in matching]
        conditions.append(
            {
                "architecture": architecture,
                "trials": len(matching),
                "passed": sum(result.recovery_passed for result in matching),
                "status_at_interruption": sorted(
                    {result.status_at_interruption for result in matching}
                ),
                "recovery_ms": {
                    "minimum": round(min(recovery_values), 6) if recovery_values else None,
                    "median": round(statistics.median(recovery_values), 6)
                    if recovery_values
                    else None,
                    "maximum": round(max(recovery_values), 6) if recovery_values else None,
                },
                "provider_calls_before_restart": sum(
                    result.provider_calls_before_restart for result in matching
                ),
                "provider_calls_after_restart": sum(
                    result.provider_calls_after_restart for result in matching
                ),
                "accepted_turns_after_restart": sum(
                    result.accepted_turns_after_restart for result in matching
                ),
            }
        )
    return {
        "experiment_id": EXPERIMENT_ID,
        "scope": "controlled interruption after durable commit and before consumer acknowledgement",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment_metadata() if metadata is None else metadata,
        "configuration": {
            "architectures": list(ARCHITECTURES),
            "repetitions_per_architecture": REPETITIONS,
            "provider": "deterministic recording provider",
            "interruption": "after SQLite commit; before result/report consumer acknowledgements",
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
            f"R1 {condition['architecture']}: {condition['passed']}/{condition['trials']} passed; "
            f"recovery median={condition['recovery_ms']['median']:.3f}ms"
        )
    print(f"Raw results: {raw_path.relative_to(ROOT)}")
    print(f"Statistics: {summary_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
