from __future__ import annotations

import math
import random
import struct
import unittest
from dataclasses import replace

from devialet_air import AirError
from devialet_air.discovery import check_ready, parse_status
from devialet_air.protocol import (
    crc16,
    encode_packet,
    parse_feedback,
    pcm_channels,
)

from .helpers import decode_audio, feedback, ready, status_packet


class ProtocolTests(unittest.TestCase):
    def test_crc_standard_vector(self) -> None:
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_silence_packet_layout(self) -> None:
        data = encode_packet([0] * 200, [0] * 200, session=123, generation=30, counter=0, mode=2)
        self.assertEqual(len(data), 166)
        self.assertEqual(decode_audio(data), ([0] * 200, [0] * 200))
        self.assertEqual(data[5:7], b"\0{")

    def test_asymmetric_random_stereo(self) -> None:
        rng = random.Random(729)
        for _ in range(30):
            left = [rng.randrange(-32768, 32768) * 77 for _ in range(200)]
            right = [rng.randrange(-5, 6) for _ in range(200)]
            packet = encode_packet(left, right, session=65535, generation=32, counter=200, mode=3)
            self.assertEqual(decode_audio(packet), (left, right))
            self.assertNotEqual(packet[27], packet[22 + 7 + (199 * packet[27] + 7) // 8 + 5])

    def test_signed_seed_and_zero_width(self) -> None:
        packet = encode_packet(
            [-123] * 200, [456] * 200, session=1, generation=32, counter=0, mode=0
        )
        self.assertEqual(decode_audio(packet), ([-123] * 200, [456] * 200))

    def test_unknown_wide_delta_rejected(self) -> None:
        with self.assertRaisesRegex(AirError, "25-bit"):
            encode_packet(
                [-(1 << 23), (1 << 23) - 1] + [0] * 198,
                [0] * 200,
                session=1,
                generation=32,
                counter=0,
                mode=0,
            )

    def test_packet_input_validation(self) -> None:
        for kwargs in ({"session": 0}, {"generation": 256}, {"counter": 1}, {"mode": 4}):
            params = dict(session=1, generation=32, counter=0, mode=2)
            params.update(kwargs)
            with self.assertRaises(AirError):
                encode_packet([0] * 200, [0] * 200, **params)

    def test_pcm_bounds_and_tail(self) -> None:
        left, right = pcm_channels(struct.pack("<hh", -32768, 32767), 1)
        self.assertEqual(left[0], round(-32768 * 77.276))
        self.assertEqual(right[0], round(32767 * 77.276))
        self.assertEqual(left[1:], [0] * 199)
        for gain in (0, -1, 1.1, math.inf, math.nan):
            with self.assertRaises(AirError):
                pcm_channels(bytes(4), gain)
        for pcm in (b"", b"\0", bytes(804)):
            with self.assertRaises(AirError):
                pcm_channels(pcm, 0.25)

    def test_feedback_mapping(self) -> None:
        parsed = parse_feedback(feedback(42, 32, 200, a=999, f=205, intervals=((1, 2),)))
        self.assertEqual(parsed.repair_counters(800), (400, 600))
        self.assertEqual(parsed.session, 42)

    def test_feedback_rejects_malformed(self) -> None:
        packet = feedback(1, 32, 200)
        for bad in (b"", packet[:-1], packet + b"\0", packet[:-1] + bytes((packet[-1] ^ 1,))):
            with self.assertRaises(AirError):
                parse_feedback(bad)

    def test_recovery_bounds(self) -> None:
        for intervals in (((2, 1),), ((1, 2), (2, 3)), ((0, 0),), ((1, 401),)):
            parsed = parse_feedback(feedback(1, 32, 0, a=999, f=0, intervals=intervals))
            with self.assertRaises(AirError):
                parsed.repair_counters(800)

    def test_status_and_readiness(self) -> None:
        status = parse_status(status_packet(), "127.0.0.1")
        self.assertEqual(status.volume_db, -28)
        self.assertEqual(status.input_labels[9], "Ethernet")
        check_ready(status, "127.0.0.1")
        for bad in (
            replace(status, muted=True),
            replace(status, powered=False),
            replace(status, received_at=0),
            replace(status, input_index=15),
        ):
            with self.assertRaises(AirError):
                check_ready(bad, "127.0.0.1")
        with self.assertRaises(AirError):
            check_ready(ready(), "127.0.0.1", 8)

    def test_ambiguous_network_input_requires_explicit_index(self) -> None:
        status = ready()
        enabled = list(status.input_enabled)
        labels = list(status.input_labels)
        enabled[8], labels[8] = True, "Wifi"
        status = replace(status, input_enabled=tuple(enabled), input_labels=tuple(labels))
        with self.assertRaises(AirError):
            check_ready(status, status.address)
        check_ready(status, status.address, 9)
