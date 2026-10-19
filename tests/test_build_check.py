from __future__ import annotations

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from tools.run_ma_trial import FRAMES, RATE, make_tone, trial_settings
from tools.stage_home_assistant_check import ROOT, stage


class BuildCheckTests(unittest.TestCase):
    def test_quiet_tone_frames_bounds_and_fades(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tone = Path(temp) / "tone.wav"
            make_tone(tone)
            with wave.open(str(tone), "rb") as source:
                self.assertEqual(source.getnframes(), FRAMES)
                self.assertEqual(source.getframerate(), RATE)
                self.assertEqual(source.getnchannels(), 2)
                self.assertEqual(source.getsampwidth(), 2)
                pcm = source.readframes(FRAMES)
            samples = struct.unpack(f"<{FRAMES * 2}h", pcm)
            self.assertEqual(samples[:2], (0, 0))
            self.assertEqual(samples[-2:], (0, 0))
            self.assertLessEqual(max(map(abs, samples)), 8192)
            self.assertGreater(max(map(abs, samples)), 8000)

    def test_trial_ports_are_separate_and_loopback(self) -> None:
        core = trial_settings("::1")["core"]
        self.assertEqual(core["webserver"]["values"]["bind_ip"], "127.0.0.1")
        self.assertEqual(core["webserver"]["values"]["bind_port"], 18095)
        self.assertEqual(core["streams"]["values"]["bind_ip"], "::1")
        self.assertEqual(core["streams"]["values"]["bind_port"], 18097)

    def test_playback_trial_defaults_to_no_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "trial"
            stage(destination, playback_trial=True)
            config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["slug"], "devialet_air_ma_trial")
            self.assertTrue(config["host_network"])
            self.assertFalse(config["options"]["confirm_playback"])
            self.assertEqual(config["boot"], "manual")
            self.assertNotIn("map", config)
            self.assertIn(
                "run_ma_trial.py", (destination / "Dockerfile").read_text(encoding="utf-8")
            )

    def test_new_context_is_manual_isolated_and_source_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "check"
            stage(destination)
            config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["boot"], "manual")
            for key in ("host_network", "homeassistant_api", "hassio_api", "auth_api"):
                self.assertFalse(config[key])
            self.assertNotIn("ports", config)
            self.assertNotIn("map", config)
            self.assertNotIn("privileged", config)
            dockerfile = (destination / "Dockerfile").read_text(encoding="utf-8")
            self.assertTrue(
                dockerfile.startswith(
                    (ROOT / "Dockerfile.music-assistant").read_text(encoding="utf-8")
                )
            )
            self.assertIn("run_container_checks.py", dockerfile)
            self.assertIn("DEVIALET_MA_IMPORT=1", dockerfile)
            self.assertEqual(
                (destination / "src" / "devialet_air" / "client.py").read_bytes(),
                (ROOT / "src" / "devialet_air" / "client.py").read_bytes(),
            )
            self.assertTrue((destination / "tests" / "test_build_check.py").is_file())
            self.assertFalse(any(destination.rglob("*.pyc")))
            self.assertFalse((destination / ".git").exists())
            self.assertFalse((destination / "dist").exists())

    def test_existing_destination_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            sentinel = destination / "keep"
            sentinel.write_bytes(b"existing app data")
            with self.assertRaisesRegex(FileExistsError, "Refusing existing"):
                stage(destination)
            self.assertEqual(sentinel.read_bytes(), b"existing app data")
            self.assertEqual(list(destination.iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
