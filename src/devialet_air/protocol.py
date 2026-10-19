"""The empirically tested Expert AIR subset, not a universal AIR specification."""

from __future__ import annotations

import binascii
import math
import struct
from dataclasses import dataclass

BLOCK_FRAMES = 200
CACHE_BLOCKS = 400
PCM_BYTES = BLOCK_FRAMES * 4
SAMPLE_SCALE = 77.276


class AirError(Exception):
    """An explicit AIR format, readiness, source, or transport failure."""


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)


def append_crc(data: bytes) -> bytes:
    return data + struct.pack(">H", crc16(data))


def check_crc(data: bytes) -> None:
    if len(data) < 2 or crc16(data[:-2]) != int.from_bytes(data[-2:], "big"):
        raise AirError("Invalid AIR CRC")


def _channel(samples: list[int]) -> bytes:
    if len(samples) != BLOCK_FRAMES or any(not -(1 << 23) <= x < (1 << 23) for x in samples):
        raise AirError("AIR requires 200 signed-24-bit samples per channel")
    deltas = [b - a for a, b in zip(samples, samples[1:], strict=False)]
    codes = [(d << 1) ^ (d >> 31) for d in deltas]
    width = max(codes, default=0).bit_length()
    if width > 24:
        raise AirError("25-bit delta receiver support is not established")
    packed = 0
    for code in codes:
        packed = (packed << width) | code
    bits = len(codes) * width
    padding = -bits % 8
    return (
        (samples[0] & 0xFFFFFF).to_bytes(3, "big")
        + bytes((0, 0, width, width))
        + (packed << padding).to_bytes((bits + 7) // 8, "big")
    )


def encode_packet(
    left: list[int],
    right: list[int],
    *,
    session: int,
    generation: int,
    counter: int,
    mode: int,
) -> bytes:
    if not 1 <= session <= 65535 or not 0 <= generation <= 255:
        raise AirError("Invalid session or generation")
    if not 0 <= counter < (1 << 64) or counter % BLOCK_FRAMES or mode not in range(4):
        raise AirError("Invalid frame counter or mode")
    header = bytearray(22)
    header[:3] = b"Dm\x02"
    struct.pack_into(">H", header, 5, session)
    header[10:12] = bytes((generation, mode))
    struct.pack_into(">QH", header, 12, counter, BLOCK_FRAMES)
    return append_crc(bytes(header) + _channel(left) + _channel(right)) + bytes(128)


def pcm_channels(pcm: bytes, gain: float) -> tuple[list[int], list[int]]:
    if not pcm or len(pcm) > PCM_BYTES or len(pcm) % 4:
        raise AirError("PCM must contain 1..200 complete PCM16LE stereo frames")
    if not math.isfinite(gain) or not 0 < gain <= 1:
        raise AirError("Gain must be finite and in (0, 1]")
    frames = list(struct.iter_unpack("<hh", pcm))
    left = [round(sample * gain * SAMPLE_SCALE) for sample, _ in frames]
    right = [round(sample * gain * SAMPLE_SCALE) for _, sample in frames]
    left.extend([0] * (BLOCK_FRAMES - len(frames)))
    right.extend([0] * (BLOCK_FRAMES - len(frames)))
    return left, right


@dataclass(frozen=True)
class Feedback:
    session: int
    generation: int
    highest: int
    contiguous: int
    receiver: int
    intervals: tuple[tuple[int, int], ...]

    def repair_counters(self, sent: int) -> tuple[int, ...]:
        b, a, f = self.contiguous, self.highest, self.receiver
        if b % 200 or b > sent or a % 200 != 199 or not b < a <= sent + 199:
            raise AirError("Invalid feedback recovery bounds")
        if f > a:
            raise AirError("Recovery receiver position exceeds highest received frame")
        counters: list[int] = []
        previous = -1
        base = f - f % 200
        for start, end in self.intervals:
            if start > end or start <= previous:
                raise AirError("Missing intervals are reversed, overlapping, or unordered")
            previous = end
            if len(counters) + end - start + 1 > CACHE_BLOCKS:
                raise AirError("Recovery request exceeds the bounded resend window")
            for index in range(start, end + 1):
                counter = base + index * 200
                if not b < counter <= sent or counter + 199 > a:
                    raise AirError("Recovery request points outside sent/received bounds")
                counters.append(counter)
        return tuple(counters)


def parse_feedback(data: bytes) -> Feedback:
    if len(data) < 41 or data[:2] != b"Df" or (len(data) - 41) % 4:
        raise AirError("Invalid feedback magic or length")
    count = struct.unpack_from(">H", data, 37)[0]
    if count > CACHE_BLOCKS or len(data) != 41 + 4 * count:
        raise AirError("Feedback interval count does not match bounded packet length")
    check_crc(data)
    return Feedback(
        session=struct.unpack_from(">H", data, 4)[0],
        generation=data[9],
        highest=struct.unpack_from(">Q", data, 10)[0],
        contiguous=struct.unpack_from(">Q", data, 18)[0],
        receiver=struct.unpack_from(">Q", data, 26)[0],
        intervals=tuple(struct.unpack_from(">HH", data, 39 + i * 4) for i in range(count)),
    )
