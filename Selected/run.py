"""Launch the layered application from this self-contained directory."""

import os
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    os.chdir(root)
    os.execv(
        sys.executable,
        [sys.executable, "-m", "streamlit", "run", "layered/streamlit_app.py", *sys.argv[1:]],
    )
