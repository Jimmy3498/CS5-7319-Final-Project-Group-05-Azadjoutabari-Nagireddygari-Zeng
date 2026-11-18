"""Run existing cross-architecture checks in a temporary development layout."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def files(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def main():
    selected, unselected = ROOT / "Selected", ROOT / "Unselected"
    for name in ("shared", "fixtures"):
        if files(selected / name) != files(unselected / name):
            raise SystemExit(f"The two {name} copies differ; update both before testing.")
    if (selected / "event_based").exists() or (unselected / "layered").exists():
        raise SystemExit("An application contains the opposite architecture.")
    with TemporaryDirectory(prefix="cs7319-check-") as temporary:
        target = Path(temporary)
        for name in ("layered", "shared", "fixtures", ".streamlit"):
            shutil.copytree(selected / name, target / name)
        shutil.copytree(unselected / "event_based", target / "event_based")
        for p in (ROOT / "Others").iterdir():
            if p.name in ("fixtures", "run_checks.py") or p.name.startswith("."):
                continue
            if p.is_dir():
                shutil.copytree(p, target / p.name)
            else:
                shutil.copy2(p, target / p.name)
        shutil.copy2(ROOT / "README.md", target / "README.md")
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        commands = [
            ["scripts/check_scaffold.py"],
            ["scripts/check_public_tree.py"],
            ["scripts/wiring_smoke.py"],
            ["-m", "unittest", "discover", "-s", "tests", "-v"],
        ]
        for arguments in commands:
            subprocess.run([sys.executable, *arguments], cwd=target, env=environment, check=True)
    print("Both submission folders and the cross-architecture regression suite passed.")


if __name__ == "__main__":
    main()
