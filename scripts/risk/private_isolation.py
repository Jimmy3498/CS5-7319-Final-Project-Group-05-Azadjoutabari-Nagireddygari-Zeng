"""P1: verify private canaries do not cross player-facing/provider boundaries."""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.risk.scenario_support import (
    CREDENTIAL_CANARY,
    PRIVATE_CANARY,
    MemoryVault,
    RecordingProvider,
    build_application,
    execute_turn,
    seed_scenario,
)
from shared.queries import Query, QueryKind

EXPERIMENT_ID = "P1"
ARCHITECTURES = ("Layered", "Event-Based")
REPETITIONS = 3
RESULTS_DIRECTORY = ROOT / "docs" / "risk" / "results"
RAW_RESULT_NAME = "P1_raw.csv"
SUMMARY_RESULT_NAME = "P1_summary.json"
RAW_FIELDS = (
    "experiment_id",
    "architecture",
    "repetition",
    "provider_calls",
    "planner_contains_private_canary",
    "narrator_contains_private_canary",
    "public_outputs_contain_private_canary",
    "trace_contains_private_canary",
    "provider_messages_contain_credential_canary",
    "public_outputs_contain_credential_canary",
    "isolation_passed",
)


@dataclass(frozen=True)
class IsolationResult:
    architecture: str
    repetition: int
    provider_calls: int
    planner_contains_private_canary: bool
    narrator_contains_private_canary: bool
    public_outputs_contain_private_canary: bool
    trace_contains_private_canary: bool
    provider_messages_contain_credential_canary: bool
    public_outputs_contain_credential_canary: bool

    @property
    def isolation_passed(self) -> bool:
        return (
            self.provider_calls == 2
            and self.planner_contains_private_canary
            and not self.narrator_contains_private_canary
            and not self.public_outputs_contain_private_canary
            and not self.trace_contains_private_canary
            and not self.provider_messages_contain_credential_canary
            and not self.public_outputs_contain_credential_canary
        )

    def raw_row(self) -> dict[str, object]:
        return {"experiment_id": EXPERIMENT_ID, **asdict(self), "isolation_passed": self.isolation_passed}


def run_trial(architecture: str, repetition: int) -> IsolationResult:
    if architecture not in ARCHITECTURES or repetition < 1:
        raise ValueError("Invalid P1 trial configuration")
    with TemporaryDirectory(prefix="p1-private-isolation-") as temporary:
        root = Path(temporary)
        provider, vault = RecordingProvider(), MemoryVault()
        app = build_application(architecture, root, provider, vault)
        try:
            scenario = seed_scenario(app, architecture, root)
            receipt = execute_turn(app, scenario, f"p1-turn-{repetition}")
            resume = app.query(
                scenario.context, Query(QueryKind.RESUME_SESSION, scenario.session_id)
            )
            report = app.query(
                scenario.context,
                Query(QueryKind.CAMPAIGN_REPORT, scenario.campaign_id, scenario.version_id),
            )
            profiles = app.query(scenario.context, Query(QueryKind.PROVIDER_CARDS))
            public_text = repr((receipt, resume, report, profiles))
            trace_text = repr(getattr(getattr(app, "bus", None), "trace", ()))
            failure_text = repr(getattr(app, "subscriber_failures", ()))
            call_texts = [call.text for call in provider.calls]
            return IsolationResult(
                architecture,
                repetition,
                len(provider.calls),
                bool(call_texts) and PRIVATE_CANARY in call_texts[0],
                len(call_texts) > 1 and PRIVATE_CANARY in call_texts[1],
                PRIVATE_CANARY in public_text,
                PRIVATE_CANARY in trace_text or PRIVATE_CANARY in failure_text,
                any(CREDENTIAL_CANARY in text for text in call_texts),
                CREDENTIAL_CANARY in public_text or CREDENTIAL_CANARY in trace_text,
            )
        finally:
            app.close()


def run_experiment(repetitions: int = REPETITIONS) -> list[IsolationResult]:
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


def build_summary(results: list[IsolationResult], metadata=None) -> dict[str, object]:
    conditions = []
    for architecture in ARCHITECTURES:
        matching = [result for result in results if result.architecture == architecture]
        conditions.append(
            {
                "architecture": architecture,
                "trials": len(matching),
                "passed": sum(result.isolation_passed for result in matching),
                "planner_received_private_canary": sum(
                    result.planner_contains_private_canary for result in matching
                ),
                "narrator_private_canary_exposures": sum(
                    result.narrator_contains_private_canary for result in matching
                ),
                "public_private_canary_exposures": sum(
                    result.public_outputs_contain_private_canary for result in matching
                ),
                "trace_private_canary_exposures": sum(
                    result.trace_contains_private_canary for result in matching
                ),
                "credential_exposures": sum(
                    result.provider_messages_contain_credential_canary
                    or result.public_outputs_contain_credential_canary
                    for result in matching
                ),
            }
        )
    return {
        "experiment_id": EXPERIMENT_ID,
        "scope": "end-to-end private-canary boundary experiment using both production gateways",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment_metadata() if metadata is None else metadata,
        "configuration": {
            "architectures": list(ARCHITECTURES),
            "repetitions_per_architecture": REPETITIONS,
            "private_canary_source": "director-only fixture record; value omitted from artifacts",
            "credential_canary_source": "in-memory credential; value omitted from artifacts",
        },
        "conditions": conditions,
        "all_trials_passed": all(result.isolation_passed for result in results),
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
        print(f"P1 {condition['architecture']}: {condition['passed']}/{condition['trials']} passed")
    print(f"Raw results: {raw_path.relative_to(ROOT)}")
    print(f"Statistics: {summary_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
