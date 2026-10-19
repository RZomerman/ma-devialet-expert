"""Independent packet decoding and a loopback-only Expert model."""

from __future__ import annotations

import asyncio
import binascii
import socket
import struct
import time

from devialet_air import ExpertStatus


def checked_crc(data: bytes) -> bytes:
    # Deliberately independent of the transport's C-accelerated CRC.
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return data + crc.to_bytes(2, "big")


def feedback(
    session: int,
    generation: int,
    b: int,
    *,
    a: int | None = None,
    f: int | None = None,
    intervals: tuple[tuple[int, int], ...] = (),
) -> bytes:
    out = bytearray(39)
    out[:2] = b"Df"
    struct.pack_into(">H", out, 4, session)
    out[9] = generation
    struct.pack_into(">QQQ", out, 10, b + 199 if a is None else a, b, b + 200 if f is None else f)
    struct.pack_into(">H", out, 37, len(intervals))
    for start, end in intervals:
        out.extend(struct.pack(">HH", start, end))
    return checked_crc(bytes(out))


def status_packet(*, index: int = 9, powered: bool = True, muted: bool = False) -> bytes:
    out = bytearray(343)
    out[:2] = b"Da"
    out[19:25] = b"Expert"
    out[52 + 9 * 17] = ord("1")
    out[53 + 9 * 17 : 53 + 9 * 17 + 8] = b"Ethernet"
    out[307] = 0x80 if powered else 0
    out[308] = index << 2 | (2 if muted else 0)
    out[310] = 139
    return checked_crc(bytes(out))


def ready() -> ExpertStatus:
    return ExpertStatus(
        "127.0.0.1",
        "Loopback Expert",
        True,
        False,
        9,
        tuple("Ethernet" if i == 9 else "" for i in range(15)),
        tuple(i == 9 for i in range(15)),
        -28,
        time.monotonic(),
    )


def decode_audio(data: bytes) -> tuple[list[int], list[int]]:
    assert data[:3] == b"Dm\x02"
    assert len(data) >= 166
    assert data[-128:] == bytes(128)
    assert binascii.crc_hqx(data[:-130], 0xFFFF) == int.from_bytes(data[-130:-128], "big")
    assert int.from_bytes(data[20:22], "big") == 200
    offset = 22
    channels = []
    for _ in range(2):
        seed = int.from_bytes(data[offset : offset + 3], "big", signed=True)
        assert data[offset + 3 : offset + 5] == b"\0\0"
        width = data[offset + 5]
        assert data[offset + 6] == width <= 24
        offset += 7
        values = [seed]
        bit_offset = offset * 8
        for _ in range(199):
            code = 0
            for _ in range(width):
                code = code * 2 + ((data[bit_offset // 8] >> (7 - bit_offset % 8)) & 1)
                bit_offset += 1
            values.append(values[-1] + ((code >> 1) ^ -(code & 1)))
        while bit_offset % 8:
            assert not (data[bit_offset // 8] >> (7 - bit_offset % 8)) & 1
            bit_offset += 1
        offset = bit_offset // 8
        channels.append(values)
    assert offset == len(data) - 130
    return channels[0], channels[1]


class ExpertSimulator:
    """Not a claim of new protocol evidence: this models documented assumptions."""

    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio.bind(("127.0.0.1", 0))
        self.audio.setblocking(False)
        self.reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.reply.bind(("127.0.0.1", 0))
        self.reply.setblocking(False)
        self.audio_port = self.audio.getsockname()[1]
        self.reply_port = self.reply.getsockname()[1]
        self.packets: dict[int, bytes] = {}
        self.attempts: dict[int, int] = {}
        self.task: asyncio.Task[None] | None = None
        self.audio_started = asyncio.Event()
        self._delayed: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> ExpertSimulator:
        self.task = asyncio.create_task(self._receive())
        return self

    async def __aexit__(self, *_: object) -> None:
        tasks = ([self.task] if self.task else []) + self._delayed
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.audio.close()
        self.reply.close()
        for result in results:
            if isinstance(result, Exception):
                raise result

    async def _receive(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, endpoint = await loop.sock_recvfrom(self.audio, 65536)
            decode_audio(data)
            session = int.from_bytes(data[5:7], "big")
            generation = data[10]
            counter = int.from_bytes(data[12:20], "big")
            if generation == 30:
                if counter == 7400 and self.mode != "startup_timeout":
                    await self._reply(feedback(session, generation, counter), endpoint)
                continue
            assert generation == 32
            self.audio_started.set()
            self.attempts[counter] = self.attempts.get(counter, 0) + 1
            if self.mode == "timeout":
                continue
            if (
                self.mode == "periodic_repair"
                and counter > 0
                and counter % 100000 == 0
                and self.attempts[counter] <= 2
            ):
                continue
            if self.mode in ("repair", "repeat_repair", "multiple_repair"):
                allowed_attempts = 2 if self.mode == "repeat_repair" else 1
                if counter == 200 and self.attempts[counter] <= allowed_attempts:
                    continue
                if (
                    self.mode == "multiple_repair"
                    and counter in (400, 800)
                    and self.attempts[counter] == 1
                ):
                    continue
            self.packets[counter] = data
            contiguous = 0
            while contiguous + 200 in self.packets:
                contiguous += 200
            if self.mode == "stall":
                contiguous = 0
            missing = ()
            if (
                self.mode in ("repair", "repeat_repair")
                and 200 not in self.packets
                and counter >= 400
            ):
                missing = ((1, 1),)
            if self.mode == "multiple_repair" and counter >= 1000:
                missing = tuple((i, i) for i in (1, 2, 4) if i * 200 not in self.packets)
            if self.mode == "periodic_repair":
                missing = tuple(
                    (i, i)
                    for i in range(500, max(self.packets) // 200 + 1, 500)
                    if i * 200 not in self.packets
                )
            packet = feedback(
                session,
                generation,
                contiguous,
                a=max(self.packets) + 199,
                f=0 if missing else max(self.packets) + 200,
                intervals=missing,
            )
            if self.mode == "corrupt":
                packet = packet[:-1] + bytes((packet[-1] ^ 1,))
            if self.mode == "foreign":
                packet = feedback(session % 65535 + 1, generation, contiguous)
            if self.mode == "no_drain":
                packet = feedback(session, generation, contiguous, f=0)
            await self._reply(packet, endpoint)
            if self.mode == "repeat_repair" and missing:
                self._delayed.append(asyncio.create_task(self._repeat_request(packet, endpoint)))

    async def _repeat_request(self, packet: bytes, endpoint: tuple[str, int]) -> None:
        await asyncio.sleep(0.02)
        await self._reply(packet, endpoint)

    async def _reply(self, packet: bytes, endpoint: tuple[str, int]) -> None:
        await asyncio.get_running_loop().sock_sendto(self.reply, packet, endpoint)
