"""Install the local AIR package and add the overlay to an offline pinned MA checkout."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

MA_COMMIT = "d941b89dbe8795443cf16f97f8406f5b37f85b9f"
MA_STABLE_COMMIT = "c24866405bd2068c25a60e1014e5ef8c3f6445bb"
SUPPORTED_MA_COMMITS = {MA_STABLE_COMMIT, MA_COMMIT}
ROOT = Path(__file__).resolve().parents[1]


def validate_destination(ma_source: Path) -> Path:
    """Reject wrong checkouts and every existing destination before installing anything."""
    source = ma_source.resolve(strict=True)
    providers = source / "music_assistant" / "providers"
    required = (
        source / "pyproject.toml",
        source / "music_assistant" / "models" / "player.py",
        providers / "__init__.py",
    )
    if not all(path.is_file() for path in required) or providers.is_symlink():
        raise ValueError("Destination must be a Music Assistant source checkout.")
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision not in SUPPORTED_MA_COMMITS:
        raise ValueError(
            "Music Assistant checkout must be pinned to 2.10.6 "
            f"({MA_STABLE_COMMIT}) or the verified development commit ({MA_COMMIT})."
        )
    destination = providers / "devialet_air"
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Refusing to replace existing path: {destination}")
    return destination


def install(ma_source: Path) -> Path:
    """Install into this interpreter and copy only the absent provider directory."""
    destination = validate_destination(ma_source)
    overlay = ROOT / "music_assistant_provider" / "devialet_air"
    if not (overlay / "manifest.json").is_file():
        raise ValueError("Local provider manifest is missing.")
    subprocess.run([sys.executable, "-m", "pip", "install", "--editable", str(ROOT)], check=True)
    # copytree refuses an existing destination, including one created after validation.
    shutil.copytree(overlay, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def main() -> None:
    """Run an explicit offline source/image-building helper, never a server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "ma_source", type=Path, help="Offline MA source checkout at the documented pin"
    )
    args = parser.parse_args()
    try:
        destination = install(args.ma_source)
    except (OSError, ValueError, subprocess.CalledProcessError) as err:
        parser.exit(1, f"Installation failed: {err}\n")
    print(f"Overlay installed at {destination}. No server was started.")


if __name__ == "__main__":
    main()
