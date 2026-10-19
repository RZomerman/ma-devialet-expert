"""Run installed-image imports and offline tests without starting Music Assistant."""

from __future__ import annotations

import json
import platform
import sys
import unittest
from importlib.metadata import version
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    print(
        "AIR_CONTAINER "
        + json.dumps(
            {
                "machine": platform.machine(),
                "python": platform.python_version(),
                "music_assistant": version("music_assistant"),
                "devialet_air": version("devialet-air"),
                "scope": "installed MA imports and loopback tests only; no MA server or amplifier",
            }
        ),
        flush=True,
    )
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
