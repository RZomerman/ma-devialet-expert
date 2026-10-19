"""Start the pinned interactive MA, optionally cloning one private MA app backup."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ORIGINAL_APP = "d5369777_music_assistant"
MAX_IMPORT_BYTES = 4 * 1024**3


def import_backup(data: Path, backups: Path, slug: str) -> None:
    """Import only regular MA data files; never restore Supervisor or other apps."""
    if not re.fullmatch(r"[a-f0-9]{8}", slug):
        raise ValueError("source_backup must be an eight-character Supervisor backup slug")
    incomplete = data / ".devialet_air_import_incomplete"
    complete = data / ".devialet_air_imported"
    if incomplete.exists():
        raise RuntimeError("An incomplete import requires explicit recovery before starting MA")
    if complete.exists():
        if complete.read_text(encoding="utf-8").strip() != slug:
            raise RuntimeError("Refusing to replace the previously imported MA profile")
        return
    if (data / "settings.json").exists():
        raise FileExistsError("Refusing to overwrite an existing MA profile")
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".air-import-", dir=data) as temporary:
        staging = Path(temporary)
        size = 0
        count = 0
        with tarfile.open(backups / f"{slug}.tar", "r:") as outer:
            member = outer.getmember(f"{ORIGINAL_APP}.tar.gz")
            if not member.isfile():
                raise ValueError("Expected a regular compressed MA app backup")
            stream = outer.extractfile(member)
            if stream is None:
                raise ValueError("MA app backup has no readable data")
            with stream, tarfile.open(fileobj=stream, mode="r|gz") as archive:
                for item in archive:
                    path = PurePosixPath(item.name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in item.name
                        or ":" in item.name
                    ):
                        raise ValueError("Unsafe archive path")
                    if not path.parts or path.parts[0] != "data":
                        continue
                    relative = path.parts[1:]
                    if not relative or relative == ("options.json",):
                        continue
                    if not item.isdir() and not item.isfile():
                        raise ValueError("Links and special files are not imported")
                    if relative[0].startswith(".devialet_air_"):
                        raise ValueError("Backup conflicts with import bookkeeping")
                    target = staging.joinpath(*relative)
                    if item.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    size += item.size
                    count += 1
                    if size > MAX_IMPORT_BYTES or count > 100000:
                        raise ValueError("MA backup exceeds the bounded import limit")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(item)
                    if source is None:
                        raise ValueError("Unreadable MA data member")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=65536)
        if not (staging / "settings.json").is_file():
            raise ValueError("Backup does not contain an MA profile")
        children = list(staging.iterdir())
        if any((data / child.name).exists() for child in children):
            raise FileExistsError("Import would overwrite existing app data")
        incomplete.write_text(slug, encoding="utf-8")
        for child in children:
            child.rename(data / child.name)
        complete.write_text(slug + "\n", encoding="utf-8")
        incomplete.unlink()
    print(f"AIR_MA_PROFILE_IMPORTED files={count} bytes={size}", flush=True)


def main() -> None:
    data = Path("/data")
    options = json.loads((data / "options.json").read_text(encoding="utf-8"))
    if slug := options["source_backup"]:
        import_backup(data, Path("/backup"), slug)
    os.execv(sys.executable, [sys.executable, "-m", "music_assistant", "--data-dir", str(data)])


if __name__ == "__main__":
    main()
