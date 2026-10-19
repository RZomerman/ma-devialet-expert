from __future__ import annotations

import asyncio
import socket
import struct
import time
import unittest
from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import patch

from devialet_air import AirClient, AirError, Discovery

from .helpers import ExpertSimulator, decode_audio, ready, status_packet


async def pcm_source(data: bytes, chunk_size: int = 333) -> AsyncIterator[bytes]:
    for pos in range(0, len(data), chunk_size):
        yield data[pos : pos + chunk_size]


def client_for(sim: ExpertSimulator, *, rate: int = 48000) -> AirClient:
    return AirClient(
        "127.0.0.1",
        "127.0.0.1",
        rate,
        _audio_port=sim.audio_port,
        _feedback_port=0,
        _feedback_source_port=sim.reply_port,
        _startup_delay=0.001,
        _timeout=0.2,
        _drain_timeout=0.3,
    )


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_rates_exact_source_frames_and_partial_tail(self) -> None:
        for rate in (44100, 48000):
            with self.subTest(rate=rate):
                async with ExpertSimulator() as sim:
                    client = client_for(sim, rate=rate)
                    source = b"".join(struct.pack("<hh", i - 200, 200 - i) for i in range(409))
                    started = []
                    stats = await client.play(
                        pcm_source(source),
                        status=ready(),
                        on_started=lambda started=started: started.append(True),
                    )
                    self.assertEqual(stats.source_frames, 409)
                    self.assertEqual(stats.sent_blocks, 3)
                    self.assertEqual(stats.acknowledged_frame, 400)
                    self.assertEqual(stats.receiver_frame, 600)
                    self.assertEqual(started, [True])
                    for block, packet in sorted(sim.packets.items()):
                        decoded = decode_audio(packet)
                        expected = [
                            (
                                round((i - 200) * 0.25 * 77.276),
                                round((200 - i) * 0.25 * 77.276),
                            )
                            if i < 409
                            else (0, 0)
                            for i in range(block, block + 200)
                        ]
                        self.assertEqual(list(zip(*decoded, strict=True)), expected)
                        self.assertEqual(packet[11], 0 if rate == 44100 else 2)

    async def test_receiver_requested_repair(self) -> None:
        for mode in ("repair", "repeat_repair", "multiple_repair"):
            with self.subTest(mode=mode):
                async with ExpertSimulator(mode) as sim:
                    client = client_for(sim)
                    result = await client.play(pcm_source(bytes(800 * 10)), status=ready())
                    self.assertGreaterEqual(result.resends, 1)
                    self.assertGreaterEqual(sim.attempts[200], 2)
                    self.assertEqual(result.acknowledged_frame, 1800)

    async def test_coarse_timer_wakeups_do_not_stretch_audio(self) -> None:
        sleep = asyncio.sleep

        async def coarse_sleep(delay: float) -> None:
            await sleep(max(0.012, delay) if delay > 0 else 0)

        async with ExpertSimulator() as sim:
            client = client_for(sim)
            started = []
            with patch("devialet_air.client.asyncio.sleep", side_effect=coarse_sleep):
                await client.play(
                    pcm_source(bytes(800 * 288)),
                    status=ready(),
                    on_started=lambda: started.append(time.perf_counter()),
                )
            elapsed = time.perf_counter() - started[0]
            self.assertGreaterEqual(elapsed, 1.18)
            self.assertLess(elapsed, 1.4)
            self.assertEqual(client.stats.sent_blocks, 288)
            self.assertEqual(client.stats.acknowledged_frame, 287 * 200)

    async def test_excessive_schedule_debt_fails_without_a_large_burst(self) -> None:
        async with ExpertSimulator() as sim:
            client = client_for(sim)
            send = client._send

            async def delayed_send(packet: bytes) -> None:
                await send(packet)
                if packet[10] == 32 and int.from_bytes(packet[12:20], "big") == 0:
                    await asyncio.sleep(0.13)

            with patch.object(client, "_send", side_effect=delayed_send):
                with self.assertRaisesRegex(AirError, "scheduling.*100 ms"):
                    await client.play(pcm_source(bytes(800 * 10)), status=ready())
            self.assertEqual(sorted(sim.packets), [0])
            self.assertFalse(client._playing)
            self.assertIsNone(client._socket)

    async def test_invalid_feedback_cannot_extend_watchdog(self) -> None:
        for mode in ("corrupt", "foreign", "timeout"):
            with self.subTest(mode=mode):
                async with ExpertSimulator(mode) as sim:
                    client = client_for(sim)
                    with self.assertRaisesRegex(AirError, "feedback"):
                        await client.play(pcm_source(bytes(800 * 80)), status=ready())
                    self.assertFalse(client._playing)
                    self.assertIsNone(client._socket)

    async def test_stalled_progress_fails_even_with_feedback(self) -> None:
        async with ExpertSimulator("stall") as sim:
            with self.assertRaisesRegex(AirError, "progress stalled"):
                await client_for(sim).play(pcm_source(bytes(800 * 100)), status=ready())

    async def test_missing_startup_ack_never_sends_audio(self) -> None:
        async with ExpertSimulator("startup_timeout") as sim:
            with self.assertRaisesRegex(AirError, "timed out"):
                await client_for(sim).play(pcm_source(bytes(800)), status=ready())
            self.assertFalse(sim.audio_started.is_set())

    async def test_final_receiver_drain_required(self) -> None:
        async with ExpertSimulator("no_drain") as sim:
            with self.assertRaises(AirError):
                await client_for(sim).play(pcm_source(bytes(800)), status=ready())

    async def test_empty_and_incomplete_source_fail(self) -> None:
        for pcm in (b"", b"\0", bytes(803)):
            with self.subTest(size=len(pcm)):
                async with ExpertSimulator() as sim:
                    with self.assertRaises(AirError):
                        await client_for(sim).play(pcm_source(pcm), status=ready())

    async def test_source_exception_propagates(self) -> None:
        async def broken() -> AsyncIterator[bytes]:
            yield bytes(800)
            raise RuntimeError("source failed")

        async with ExpertSimulator() as sim:
            with self.assertRaisesRegex(RuntimeError, "source failed"):
                await client_for(sim).play(broken(), status=ready())

    async def test_stalled_source_is_bounded(self) -> None:
        async def stalled() -> AsyncIterator[bytes]:
            yield bytes(800)
            await asyncio.Event().wait()

        async with ExpertSimulator() as sim:
            with self.assertRaises(AirError):
                await client_for(sim).play(stalled(), status=ready())

    async def test_cancel_closes_generator_and_socket(self) -> None:
        closed = asyncio.Event()

        async def endless() -> AsyncIterator[bytes]:
            try:
                while True:
                    yield bytes(800)
            finally:
                closed.set()

        async with ExpertSimulator() as sim:
            client = client_for(sim)
            task = asyncio.create_task(client.play(endless(), status=ready()))
            await asyncio.wait_for(sim.audio_started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
            self.assertFalse(client._playing)
            self.assertIsNone(client._socket)

    async def test_stop_during_blocked_first_read(self) -> None:
        entered = asyncio.Event()

        async def blocked() -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async with ExpertSimulator() as sim:
            task = asyncio.create_task(client_for(sim).play(blocked(), status=ready()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertFalse(sim.audio_started.is_set())

    async def test_readiness_rechecked_after_source_acquisition(self) -> None:
        async with ExpertSimulator() as sim:
            with self.assertRaisesRegex(AirError, "unmuted"):
                await client_for(sim).play(
                    pcm_source(bytes(800)),
                    status=ready(),
                    status_provider=lambda: replace(ready(), muted=True),
                )
            self.assertFalse(sim.audio_started.is_set())

    async def test_first_pcm_deadline_is_bounded(self) -> None:
        async def delayed() -> AsyncIterator[bytes]:
            await asyncio.sleep(0.1)
            yield bytes(800)

        async with ExpertSimulator() as sim:
            client = client_for(sim)
            client._source_start_timeout = 0.01
            with self.assertRaisesRegex(AirError, "timed out"):
                await client.play(delayed(), status=ready())
            self.assertFalse(sim.audio_started.is_set())

    async def test_reconnect_uses_fresh_session(self) -> None:
        sessions = []
        async with ExpertSimulator() as sim:
            client = client_for(sim)
            for _ in range(2):
                await client.play(pcm_source(bytes(800)), status=ready())
                sessions.append(client._session)
            self.assertNotEqual(sessions[0], sessions[1])

    async def test_large_source_chunk_rejected(self) -> None:
        async with ExpertSimulator() as sim:
            with self.assertRaisesRegex(AirError, "1 MiB"):
                await client_for(sim).play(pcm_source(bytes(1048577), 1048577), status=ready())

    async def test_cache_bound_and_steady_mode_transition(self) -> None:
        async with ExpertSimulator() as sim:
            client = client_for(sim)
            result = await client.play(pcm_source(bytes(800 * 405)), status=ready())
            self.assertEqual(result.source_frames, 81000)
            self.assertEqual(len(client._cache), 400)
            self.assertEqual(sim.packets[249 * 200][11], 2)
            self.assertEqual(sim.packets[250 * 200][11], 3)

    async def test_backpressure_bounds_source_read_ahead(self) -> None:
        produced = 0

        async def source() -> AsyncIterator[bytes]:
            nonlocal produced
            for _ in range(10000):
                produced += 1
                yield bytes(800)

        async with ExpertSimulator("startup_timeout") as sim:
            client = client_for(sim)
            task = asyncio.create_task(client.play(source(), status=ready()))
            await asyncio.sleep(0.03)
            # 32 queued blocks, one held by startup and one pending queue.put.
            self.assertLessEqual(produced, 34)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_exclusive_feedback_bind(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            async with ExpertSimulator() as sim:
                client = client_for(sim)
                client._feedback_port = occupied.getsockname()[1]
                with self.assertRaisesRegex(AirError, "socket failure"):
                    await client.play(pcm_source(bytes(800)), status=ready())
                self.assertFalse(sim.audio_started.is_set())


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_udp_status_and_readiness(self) -> None:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.bind(("127.0.0.1", 0))
        sender.setblocking(False)
        # Production uses the same status source/destination port on different hosts.
        discovery = Discovery(local_address="127.0.0.2", port=sender.getsockname()[1])
        await discovery.start()
        try:
            await asyncio.get_running_loop().sock_sendto(
                sender, status_packet(), ("127.0.0.2", discovery.port)
            )
            status = await discovery.wait_for_ready("127.0.0.1", timeout=1)
            self.assertEqual(status.name, "Expert")
            self.assertEqual(discovery.require_ready("127.0.0.1"), status)
        finally:
            await discovery.close()
            sender.close()

    async def test_discovery_timeout_honors_full_deadline(self) -> None:
        discovery = Discovery(local_address="127.0.0.1", port=0)
        await discovery.start()
        start = time.perf_counter()
        try:
            with self.assertRaises(TimeoutError):
                await discovery.wait_for_ready("127.0.0.2", timeout=0.25)
            # Windows event-loop timers may wake one coarse clock tick early.
            self.assertGreaterEqual(time.perf_counter() - start, 0.23)
        finally:
            await discovery.close()
