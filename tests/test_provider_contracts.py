"""Optional real-model checks and non-mutating deployment-helper tests."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MA_SOURCE = Path(os.environ.get("DEVIALET_MA_SOURCE", ROOT.parent / "music-assistant-server"))


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER = load_file("_air_install_test", ROOT / "tools" / "install_provider.py")


@unittest.skipUnless(
    sys.version_info >= (3, 14) and MA_SOURCE.is_dir(),
    "Optional pinned MA source interfaces require Python 3.14 and the sibling checkout",
)
class PinnedSourceInterfaceTests(unittest.TestCase):
    def test_actual_player_provider_and_stream_interfaces(self):
        source = MA_SOURCE
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        self.assertIn(commit, INSTALLER.SUPPORTED_MA_COMMITS)

        def methods(relative, class_name):
            tree = ast.parse((source / relative).read_text(encoding="utf-8"))
            cls = next(
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            )
            return {
                node.name: [arg.arg for arg in node.args.args]
                for node in cls.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }

        provider = methods("music_assistant/models/provider.py", "Provider")
        player_provider = methods("music_assistant/models/player_provider.py", "PlayerProvider")
        player = methods("music_assistant/models/player.py", "Player")
        streams = methods("music_assistant/controllers/streams/controller.py", "StreamsController")
        session = methods("music_assistant/models/setup_flow.py", "SetupSession")
        self.assertEqual(provider["signal_provider_event"], ["self", "data", "sub_scope"])
        self.assertIn("discover_players", player_provider)
        self.assertIn("loaded_in_mass", player_provider)
        self.assertEqual(player["play_media"], ["self", "media"])
        self.assertEqual(player["stop"], ["self"])
        self.assertEqual(player["volume_set"], ["self", "volume_level"])
        self.assertEqual(player["power"], ["self", "powered"])
        self.assertEqual(player["on_unload"], ["self"])
        self.assertEqual(player["update_state"], ["self", "force_update", "signal_event"])
        self.assertEqual(
            streams["get_stream"], ["self", "media", "pcm_format", "player_id", "force_flow_mode"]
        )
        self.assertIn("form", session)
        self.assertIn("finish", session)


class InstallerTests(unittest.TestCase):
    def test_wrong_checkout_fails_before_pip_or_copy(self):
        with patch.object(INSTALLER.subprocess, "run") as pip:
            with self.assertRaises(ValueError):
                INSTALLER.install(ROOT)
            pip.assert_not_called()

    def test_existing_destination_is_never_replaced(self):
        source = Mock(spec=Path)
        source.resolve.return_value = source
        paths = {}

        def path(name):
            if name not in paths:
                result = Mock(spec=Path)
                result.__truediv__ = Mock(side_effect=lambda child: path(f"{name}/{child}"))
                result.is_file.return_value = True
                result.is_symlink.return_value = False
                result.exists.return_value = name.endswith("/providers/devialet_air")
                paths[name] = result
            return paths[name]

        source.__truediv__ = Mock(side_effect=lambda name: path(name))
        with patch.object(INSTALLER.subprocess, "check_output", return_value=INSTALLER.MA_COMMIT):
            with self.assertRaisesRegex(ValueError, "Refusing to replace"):
                INSTALLER.validate_destination(source)

    def test_wrong_pin_is_rejected(self):
        source = ROOT.parent / "music-assistant-server"
        if not source.is_dir():
            self.skipTest("Optional sibling MA checkout is not present")
        with patch.object(INSTALLER.subprocess, "check_output", return_value="wrong"):
            with self.assertRaisesRegex(ValueError, "must be pinned"):
                INSTALLER.validate_destination(source)

    def test_helper_only_installs_package_then_absent_overlay(self):
        destination = ROOT / "not-created"
        with (
            patch.object(INSTALLER, "validate_destination", return_value=destination),
            patch.object(INSTALLER.subprocess, "run") as pip,
            patch.object(INSTALLER.shutil, "copytree") as copy,
        ):
            self.assertEqual(INSTALLER.install(ROOT), destination)
        pip.assert_called_once_with(
            [sys.executable, "-m", "pip", "install", "--editable", str(ROOT)], check=True
        )
        self.assertEqual(copy.call_args.args[1], destination)
        self.assertNotIn("dirs_exist_ok", copy.call_args.kwargs)


@unittest.skipUnless(
    importlib.util.find_spec("music_assistant_models"),
    "Optional exact MA models are not installed in this interpreter",
)
class RealModelTests(unittest.TestCase):
    def test_actual_player_device_info_constructor(self):
        from music_assistant_models.enums import PlayerFeature
        from music_assistant_models.player import DeviceInfo

        self.assertEqual(PlayerFeature.VOLUME_SET.value, "volume_set")
        self.assertEqual(PlayerFeature.POWER.value, "power")

        tree = ast.parse(
            (ROOT / "music_assistant_provider" / "devialet_air" / "player.py").read_text()
        )
        call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "DeviceInfo"
        )
        inspect.signature(DeviceInfo).bind(**{keyword.arg: None for keyword in call.keywords})

    def test_actual_configuration_and_manifest_models(self):
        from music_assistant_models.enums import ProviderStage, ProviderType
        from music_assistant_models.provider import ProviderManifest

        config = load_file(
            "_air_real_config",
            ROOT / "music_assistant_provider" / "devialet_air" / "config.py",
        )
        entries = config.config_entries()
        for entry in entries:
            entry.to_dict()
        rate = next(entry for entry in entries if entry.key == "sample_rate")
        self.assertEqual([option.value for option in rate.options], [48000, 44100])
        manifest = ProviderManifest.from_dict(
            json.loads(
                (ROOT / "music_assistant_provider" / "devialet_air" / "manifest.json").read_text()
            )
        )
        self.assertEqual(manifest.type, ProviderType.PLAYER)
        self.assertEqual(manifest.stage, ProviderStage.ALPHA)
        self.assertFalse(manifest.multi_instance)
        self.assertEqual(manifest.requirements, [])


@unittest.skipUnless(
    os.environ.get("DEVIALET_MA_IMPORT") == "1",
    "Set DEVIALET_MA_IMPORT=1 on a supported MA runtime for a real provider import",
)
class RealImportTests(unittest.TestCase):
    def test_actual_provider_import_without_test_doubles(self):
        code = """
import importlib.util
import sys
from pathlib import Path
path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "music_assistant.providers.devialet_air", path,
    submodule_search_locations=[str(path.parent)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.SUPPORTED_FEATURES == set()
"""
        subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(ROOT / "music_assistant_provider" / "devialet_air" / "__init__.py"),
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
