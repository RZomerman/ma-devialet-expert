"""Cancellable user-mode transport with bounded input, repair, and explicit failure."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import secrets
import socket
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import dataclass

from .discovery import ExpertStatus, check_ready
from .protocol import (
    BLOCK_FRAMES,
    CACHE_BLOCKS,
    PCM_BYTES,
    AirError,
    encode_packet,
    parse_feedback,
    pcm_channels,
)

LOGGER = logging.getLogger(__name__)
MAX_SOURCE_CHUNK = 1024 * 1024
MAX_SEND_LATENESS_SECONDS = 0.1


@dataclass
class PlaybackStats:
    sample_rate: int
    source_frames: int = 0
    sent_blocks: int = 0
    acknowledged_frame: int = -1
    receiver_frame: int = 0
    resends: int = 0
    invalid_feedback: int = 0
    max_lateness_seconds: float = 0
    elapsed_seconds: float = 0


class AirClient:
    def __init__(
        self,
        expert_address: str,
        local_address: str | None = None,
        sample_rate: int = 48000,
        gain: float = 0.25,
        *,
        source_start_timeout: float = 30,
        _audio_port: int = 45455,
        _feedback_port: int = 45456,
        _feedback_source_port: int = 45456,
        _startup_delay: float = 1,
        _timeout: float = 2,
        _drain_timeout: float = 3,
    ) -> None:
        self.expert_address = str(ipaddress.IPv4Address(expert_address))
        self.local_address = str(ipaddress.IPv4Address(local_address)) if local_address else None
        if sample_rate not in (44100, 48000):
            raise AirError("Only 44100 and 48000 Hz are supported")
        if not math.isfinite(gain) or not 0 < gain <= 1:
            raise AirError("Gain must be finite and in (0, 1]")
        if not math.isfinite(source_start_timeout) or source_start_timeout <= 0:
            raise AirError("Source startup timeout must be positive and finite")
        self.sample_rate = sample_rate
        self.gain = gain
        self.stats = PlaybackStats(sample_rate)
        self._audio_port = _audio_port
        self._source_start_timeout = source_start_timeout
        self._feedback_port = _feedback_port
        self._feedback_source_port = _feedback_source_port
        self._startup_delay = _startup_delay
        self._timeout = _timeout
        self._drain_timeout = _drain_timeout
        self._playing = False
        self._socket: socket.socket | None = None
        self._session = 0
        self._audio_started = False
        self._ready = asyncio.Event()
        self._progress = asyncio.Event()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._pending: set[int] = set()
        self._last_feedback = 0.0
        self._last_progress = 0.0
        self._started_at = 0.0
        self._on_progress: Callable[[PlaybackStats], None] | None = None

    async def play(
        self,
        source: AsyncIterable[bytes],
        *,
        status: ExpertStatus,
        network_input_index: int | None = None,
        status_provider: Callable[[], ExpertStatus] | None = None,
        on_started: Callable[[], None] | None = None,
        on_progress: Callable[[PlaybackStats], None] | None = None,
    ) -> PlaybackStats:
        if self._playing:
            raise AirError("This AIR client is already playing")
        check_ready(status, self.expert_address, network_input_index)
        self._playing = True
        self.stats = PlaybackStats(self.sample_rate)
        self._audio_started = False
        self._ready.clear()
        self._cache.clear()
        self._pending.clear()
        session = secrets.randbelow(65535) + 1
        self._session = session if session != self._session else session % 65535 + 1
        self._on_progress = on_progress
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tasks: list[asyncio.Task[None]] = []
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            local = self.local_address
            if local is None:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                    route.connect((self.expert_address, self._audio_port))
                    local = str(route.getsockname()[0])
            sock.bind((local, self._feedback_port))
            sock.setblocking(False)
            self._socket = sock
            producer = asyncio.create_task(self._produce(source, queue), name="air-pcm")
            receiver = asyncio.create_task(self._receive(sock), name="air-feedback")
            watchdog = asyncio.create_task(self._watchdog(), name="air-health")
            runner = asyncio.create_task(
                self._run(queue, on_started, status, network_input_index, status_provider),
                name="air-send",
            )
            tasks.extend((producer, receiver, watchdog, runner))
            active = set(tasks)
            while True:
                done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                # A successful sender must not hide a simultaneous background failure.
                for task in done:
                    task.result()
                if runner in done:
                    return self.stats
                if receiver in done or watchdog in done:
                    raise AirError("AIR background worker stopped unexpectedly")
        except TimeoutError as error:
            raise AirError("AIR source, startup, or completion timed out") from error
        except OSError as error:
            raise AirError(f"AIR socket failure: {error}") from error
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            sock.close()
            self._socket = None
            self._playing = False

    async def _produce(
        self, source: AsyncIterable[bytes], queue: asyncio.Queue[bytes | None]
    ) -> None:
        try:
            await self._produce_blocks(source, queue)
        finally:
            if isinstance(source, AsyncGenerator):
                await source.aclose()

    async def _produce_blocks(
        self, source: AsyncIterable[bytes], queue: asyncio.Queue[bytes | None]
    ) -> None:
        pending = bytearray()
        async for chunk in source:
            if not isinstance(chunk, bytes) or len(chunk) > MAX_SOURCE_CHUNK:
                raise AirError("PCM source must yield bytes chunks no larger than 1 MiB")
            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                count = min(PCM_BYTES - len(pending), len(view) - offset)
                pending.extend(view[offset : offset + count])
                offset += count
                if len(pending) == PCM_BYTES:
                    await queue.put(bytes(pending))
                    pending.clear()
        if len(pending) % 4:
            raise AirError("PCM source ended with an incomplete stereo frame")
        if pending:
            await queue.put(bytes(pending))
        await queue.put(None)

    async def _send(self, data: bytes) -> None:
        if self._socket is None:
            raise AirError("AIR socket is not open")
        await asyncio.get_running_loop().sock_sendto(
            self._socket, data, (self.expert_address, self._audio_port)
        )

    async def _receive(self, sock: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, endpoint = await loop.sock_recvfrom(sock, 65536)
            if endpoint != (self.expert_address, self._feedback_source_port):
                self._invalid("foreign endpoint")
                continue
            try:
                feedback = parse_feedback(data)
            except AirError as error:
                self._invalid(str(error))
                continue
            if feedback.session != self._session:
                self._invalid("foreign session")
                continue
            if not self._audio_started:
                if (
                    feedback.generation == 30
                    and feedback.contiguous == 7400
                    and feedback.highest == 7599
                    and not feedback.intervals
                ):
                    self._ready.set()
                continue
            sent = (self.stats.sent_blocks - 1) * 200
            b = feedback.contiguous
            if (
                feedback.generation != 32
                or b % 200
                or not self.stats.acknowledged_frame <= b <= sent
                or feedback.highest % 200 != 199
                or not b < feedback.highest <= sent + 199
            ):
                self._invalid("generation or progress bounds")
                continue
            counters = feedback.repair_counters(sent) if feedback.intervals else ()
            for counter in counters:
                if counter not in self._cache:
                    raise AirError("Requested recovery packet has expired from the resend cache")
            self._pending.update(counters)
            self._pending.difference_update(c for c in self._pending.copy() if c <= b)
            now = time.perf_counter()
            self._last_feedback = now
            if b > self.stats.acknowledged_frame:
                self._last_progress = now
            self.stats.acknowledged_frame = b
            self.stats.receiver_frame = feedback.receiver
            self.stats.elapsed_seconds = max(0, now - self._started_at)
            self._progress.set()
            if self._on_progress:
                self._on_progress(self.stats)

    def _invalid(self, reason: str) -> None:
        self.stats.invalid_feedback += 1
        if self.stats.invalid_feedback == 1 or self.stats.invalid_feedback % 100 == 0:
            LOGGER.warning("Rejected AIR feedback (%s): %s", self.stats.invalid_feedback, reason)

    async def _watchdog(self) -> None:
        period = BLOCK_FRAMES / self.sample_rate
        while True:
            await asyncio.sleep(period)
            if not self._audio_started:
                continue
            now = time.perf_counter()
            if now - self._last_feedback >= self._timeout:
                raise AirError("No valid AIR feedback within the watchdog deadline")
            sent = (self.stats.sent_blocks - 1) * 200
            if sent > self.stats.acknowledged_frame and now - self._last_progress >= self._timeout:
                raise AirError("AIR reception progress stalled; automatic restart is not safe")
            if self._pending:
                counter = min(self._pending)
                self._pending.remove(counter)
                packet = self._cache.get(counter)
                if packet is None:
                    raise AirError("Queued recovery packet expired from the resend cache")
                await self._send(packet)
                self.stats.resends += 1

    async def _run(
        self,
        queue: asyncio.Queue[bytes | None],
        on_started: Callable[[], None] | None,
        status: ExpertStatus,
        network_input_index: int | None,
        status_provider: Callable[[], ExpertStatus] | None,
    ) -> None:
        async with asyncio.timeout(self._source_start_timeout):
            first = await queue.get()
        if first is None:
            raise AirError("PCM source is empty; no startup was sent")
        check_ready(
            status_provider() if status_provider else status,
            self.expert_address,
            network_input_index,
        )
        silence = [0] * BLOCK_FRAMES
        initial_mode = 0 if self.sample_rate == 44100 else 2
        period = BLOCK_FRAMES / self.sample_rate
        for block in range(38):
            await self._send(
                encode_packet(
                    silence,
                    silence,
                    session=self._session,
                    generation=30,
                    counter=block * 200,
                    mode=initial_mode,
                )
            )
            await asyncio.sleep(period)
        try:
            async with asyncio.timeout(self._timeout):
                await self._ready.wait()
        except TimeoutError as error:
            raise AirError("AIR startup was not acknowledged for the new session") from error
        await asyncio.sleep(self._startup_delay)
        self._started_at = self._last_feedback = self._last_progress = time.perf_counter()
        self._audio_started = True
        if on_started:
            on_started()
        pcm: bytes | None = first
        deadline = time.perf_counter()
        while pcm is not None:
            await asyncio.sleep(max(0, deadline - time.perf_counter()))
            while (remaining := deadline - time.perf_counter()) > 0:  # noqa: ASYNC110 - Timers may wake early.
                await asyncio.sleep(remaining)
            now = time.perf_counter()
            self.stats.max_lateness_seconds = max(self.stats.max_lateness_seconds, now - deadline)
            if now - deadline > MAX_SEND_LATENESS_SECONDS:
                raise AirError("AIR send scheduling fell more than 100 ms behind the audio clock")
            # Keep the source clock despite timer jitter; yield between catch-up packets.
            deadline += period
            block = self.stats.sent_blocks
            left, right = pcm_channels(pcm, self.gain)
            packet = encode_packet(
                left,
                right,
                session=self._session,
                generation=32,
                counter=block * 200,
                mode=initial_mode + (block >= (227 if self.sample_rate == 44100 else 250)),
            )
            self._cache[block * 200] = packet
            if len(self._cache) > CACHE_BLOCKS:
                self._cache.popitem(last=False)
            self.stats.sent_blocks += 1
            self.stats.source_frames += len(pcm) // 4
            await self._send(packet)
            async with asyncio.timeout(self._timeout):
                pcm = await queue.get()
        sent = (self.stats.sent_blocks - 1) * 200
        async with asyncio.timeout(self._drain_timeout):
            while self.stats.acknowledged_frame < sent:
                self._progress.clear()
                await self._progress.wait()
        async with asyncio.timeout(self._drain_timeout):
            while self.stats.receiver_frame < sent + 200:
                self._progress.clear()
                await self._progress.wait()
