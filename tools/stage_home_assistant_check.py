"""Stage a separate, manual-start Home Assistant app for offline runtime checks."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "name": "Devialet AIR Build Check",
    "version": "0.1.0",
    "slug": "devialet_air_build_check",
    "description": "Offline MA 2.10.6 import and loopback tests; no server or audio output.",
    "arch": ["aarch64", "amd64"],
    "startup": "application",
    "boot": "manual",
    "init": True,
    "host_network": False,
    "homeassistant_api": False,
    "hassio_api": False,
    "auth_api": False,
    "options": {},
    "schema": {},
}
DOCKER_TAIL = """
COPY Dockerfile.music-assistant .dockerignore /opt/devialet-air/
COPY tests /opt/devialet-air/tests
COPY tools /opt/devialet-air/tools
COPY music_assistant_provider /opt/devialet-air/music_assistant_provider
ENV DEVIALET_MA_IMPORT=1 DEVIALET_AIR_SOAK_SECONDS=15
WORKDIR /opt/devialet-air
ENTRYPOINT ["/app/venv/bin/python", "-u", "/opt/devialet-air/tools/run_container_checks.py"]
CMD []
"""
CONTEXT_TAIL = """
!.dockerignore
!tests/
!tests/*.py
!tools/
!tools/*.py
"""


def _copy_source(source: Path, target: Path) -> None:
    """Copy with bounded reads, without platform metadata or offload requirements."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer)


def stage(
    destination: Path, *, playback_trial: bool = False, interactive_server: bool = False
) -> None:
    """Create only a new context; never overwrite an existing app or deployment."""
    if playback_trial and interactive_server:
        raise ValueError("Choose either one-shot trial or interactive server")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Refusing existing build-check destination: {destination}")
    destination.mkdir()
    for name in ("pyproject.toml", "Dockerfile.music-assistant", "README.md", "LICENSE", "NOTICE"):
        _copy_source(ROOT / name, destination / name)
    for directory, suffixes in (
        ("src", {".py", ".typed"}),
        ("music_assistant_provider", {".py", ".json"}),
        ("tests", {".py"}),
        ("tools", {".py"}),
    ):
        for source in sorted((ROOT / directory).rglob("*")):
            if source.is_file() and source.suffix in suffixes:
                target = destination / source.relative_to(ROOT)
                _copy_source(source, target)
    tail = DOCKER_TAIL
    config = dict(CONFIG)
    if playback_trial:
        tail = tail.replace("run_container_checks.py", "run_ma_trial.py")
        config.update(
            name="Devialet AIR MA Playback Trial",
            slug="devialet_air_ma_trial",
            description="One-shot actual MA trial; audio requires explicit confirmation.",
            host_network=True,
            options={
                "expert": "",
                "network_input_index": 9,
                "attempt_id": "initial",
                "confirm_playback": False,
            },
            schema={
                "expert": "str",
                "network_input_index": "int(0,14)",
                "attempt_id": "str",
                "confirm_playback": "bool",
            },
        )
    if interactive_server:
        tail = DOCKER_TAIL.replace("run_container_checks.py", "run_ma_server.py")
        config.update(
            name="Music Assistant 2.10.6 - Devialet AIR",
            slug="devialet_air_server",
            description="Interactive pinned MA with AIR; optional private MA backup import.",
            host_network=True,
            discovery=["music_assistant"],
            homeassistant_api=True,
            hassio_api=True,
            hassio_role="manager",
            auth_api=True,
            ingress=True,
            ingress_port=8094,
            apparmor=True,
            map=[
                {"type": "backup", "read_only": True},
                {"type": "media", "read_only": True},
            ],
            privileged=["SYS_ADMIN", "DAC_READ_SEARCH"],
            options={"source_backup": ""},
            schema={"source_backup": "str"},
        )
        (destination / "apparmor.txt").write_text(
            "profile devialet_air_server flags=(attach_disconnected,mediate_deleted) {\n"
            "  capability,\n  file,\n  signal,\n  network,\n"
            "  mount fstype=cifs,\n  umount,\n}\n",
            encoding="utf-8",
        )
    (destination / "Dockerfile").write_text(
        (ROOT / "Dockerfile.music-assistant").read_text(encoding="utf-8") + tail,
        encoding="utf-8",
    )
    (destination / ".dockerignore").write_text(
        (ROOT / ".dockerignore").read_text(encoding="utf-8") + CONTEXT_TAIL,
        encoding="utf-8",
    )
    (destination / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New folder in the existing HA addons share")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--playback-trial", action="store_true", help="Stage the opt-in MA trial")
    modes.add_argument("--interactive-server", action="store_true", help="Stage interactive MA")
    args = parser.parse_args()
    stage(
        args.destination,
        playback_trial=args.playback_trial,
        interactive_server=args.interactive_server,
    )
    print(f"Staged offline build-check app at {args.destination}; nothing installed or started.")
