"""Cross-platform bootstrap and launcher.

One command, every OS. Creates a .venv next to this file on first run,
installs Flask into it, then starts the web UI. Subsequent runs reuse
the existing .venv. Designed so a new user can clone the repo and type
a single Python command, no shell-specific activation, no path quirks.

Usage:
    python3 run.py            # macOS, Linux
    python run.py             # Windows

Args after `run.py` are forwarded to the underlying CLI, so:
    python3 run.py stats
    python3 run.py web --new 30
    python3 run.py review
all work the same way `./dutch` does on macOS or `.\\dutch.cmd` on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
APP_CLI = ROOT / "app" / "cli.py"


def venv_python() -> Path:
    """Return the Python executable inside the local .venv."""
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> None:
    """Create the venv and install requirements if either is missing.

    The check is two-staged: presence of the venv directory and presence
    of Flask inside it. A half-built venv (interrupted install) gets
    finished off rather than left in a broken state.
    """
    if not VENV_DIR.exists():
        print(f"creating virtual environment at {VENV_DIR.name} ...")
        venv.create(VENV_DIR, with_pip=True)

    py = venv_python()
    if not py.exists():
        # Should not normally happen, but guard anyway. Re-creating is safe.
        print("venv looks corrupt, rebuilding ...")
        venv.create(VENV_DIR, with_pip=True)

    # Quick probe: is Flask importable inside the venv? If yes, skip pip.
    probe = subprocess.run(
        [str(py), "-c", "import flask"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        print("installing dependencies (one-time, takes a few seconds) ...")
        subprocess.check_call(
            [str(py), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)]
        )


def main() -> int:
    if not REQUIREMENTS.exists() or not APP_CLI.exists():
        print(
            "run.py expected to live in the repo root next to requirements.txt "
            "and app/cli.py. Did you cd into the project directory?",
            file=sys.stderr,
        )
        return 2

    ensure_venv()

    # Default subcommand is `web` so the bare `python run.py` opens the UI.
    forwarded = sys.argv[1:] or ["web"]
    return subprocess.call([str(venv_python()), str(APP_CLI), *forwarded])


if __name__ == "__main__":
    raise SystemExit(main())
