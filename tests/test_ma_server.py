"""Private profile migration guards; all archive contents here are synthetic."""

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.run_ma_server import ORIGINAL_APP, import_backup
from tools.stage_home_assistant_check import stage


class ServerTests(unittest.TestCase):
    def make_backup(self, root, extra=None):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            entries = {"data/settings.json": b'{"test":true}', "data/options.json": b"old"}
            entries.update(extra or {})
            for name, value in entries.items():
                item = tarfile.TarInfo(name)
                item.size = len(value)
                archive.addfile(item, io.BytesIO(value))
        with tarfile.open(root / "1234abcd.tar", "w") as outer:
            item = tarfile.TarInfo(f"{ORIGINAL_APP}.tar.gz")
            item.size = len(payload.getvalue())
            outer.addfile(item, io.BytesIO(payload.getvalue()))

    def test_import_preserves_supervisor_options_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "options.json").write_text("new")
            self.make_backup(root)
            import_backup(data, root, "1234abcd")
            import_backup(data, root, "1234abcd")
            self.assertEqual((data / "options.json").read_text(), "new")
            self.assertTrue((data / "settings.json").is_file())
            with self.assertRaises(RuntimeError):
                import_backup(data, root, "abcd1234")

    def test_unsafe_archive_never_installs_profile(self):
        for name in ("data/../escape", "/data/escape", "data\\escape", "data/C:/escape"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.make_backup(root, {name: b"bad"})
                with self.assertRaises(ValueError):
                    import_backup(root / "data", root, "1234abcd")
                self.assertFalse((root / "data" / "settings.json").exists())

    def test_existing_profile_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / "settings.json").write_text("keep")
            with self.assertRaises(FileExistsError):
                import_backup(data, data, "1234abcd")
            self.assertEqual((data / "settings.json").read_text(), "keep")

    def test_interactive_app_has_readonly_mounts_and_confined_smb_support(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "context"
            stage(target, interactive_server=True)
            config = json.loads((target / "config.json").read_text())
            self.assertEqual(config["boot"], "manual")
            self.assertTrue(all(mapping["read_only"] for mapping in config["map"]))
            self.assertTrue(config["hassio_api"])
            self.assertTrue(config["apparmor"])
            self.assertEqual(config["discovery"], ["music_assistant"])
            self.assertIn("mount fstype=cifs", (target / "apparmor.txt").read_text())
            self.assertIn("run_ma_server.py", (target / "Dockerfile").read_text())
            for name in ("README.md", "LICENSE", "NOTICE"):
                self.assertTrue((target / name).is_file())
                self.assertIn(f"!{name}", (target / ".dockerignore").read_text())
