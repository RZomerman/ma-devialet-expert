"""Cross-check against actual C# definitions without executing their live wrapper."""

from __future__ import annotations

import hashlib
import os
import subprocess
import unittest
from pathlib import Path

from devialet_air.protocol import encode_packet


@unittest.skipUnless(
    os.name == "nt"
    and (Path(__file__).resolve().parent / "verify_reference.ps1").is_file()
    and (Path(__file__).resolve().parents[1] / "air-protocol-research" / "Send-Mp3.ps1").is_file(),
    "Optional external C# reference requires Windows PowerShell and research fixtures",
)
class ReferenceTests(unittest.TestCase):
    def test_existing_csharp_encoder_matches_python(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-File",
                str(root / "tests" / "verify_reference.ps1"),
                "-SenderPath",
                str(root / "air-protocol-research" / "Send-Mp3.ps1"),
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = []
        for rate in (44100, 48000):
            for index in (0, 37):
                packet = encode_packet(
                    [0] * 200,
                    [0] * 200,
                    session=12345,
                    generation=30,
                    counter=index * 200,
                    mode=0 if rate == 44100 else 2,
                )
                expected.append(f"{rate}/{index}/{hashlib.sha256(packet).hexdigest()}")
        packet = encode_packet(
            [(i - 100) * 77 for i in range(200)],
            [100 - i for i in range(200)],
            session=12345,
            generation=32,
            counter=0,
            mode=3,
        )
        expected.append(f"stereo/{hashlib.sha256(packet).hexdigest()}")
        self.assertEqual(result.stdout.splitlines(), expected)
