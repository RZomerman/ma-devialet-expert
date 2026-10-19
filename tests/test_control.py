"""Offline hardware-volume protocol, confirmation and MA contract checks."""

from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from devialet_air import AirError, Discovery
from devialet_air.control import power_packet, volume_db, volume_level, volume_packet
from devialet_air.protocol import crc16
from tests.test_provider import CONFIG, PLAYER, FakeMass, settings, status


class VolumeCodecTests(unittest.TestCase):
    def test_slider_bounds_and_roundtrip(self):
        for maximum in (-60, -10, 0):
            for level in range(101):
                db = volume_db(level, maximum)
                self.assertTrue(-96.5 <= db <= maximum)
                self.assertEqual(db * 2, round(db * 2))
                self.assertLessEqual(abs(volume_level(db, maximum) - level), 1)
        self.assertEqual(volume_db(0, -10), -96.5)
        self.assertEqual(volume_db(100, -10), -10)
        self.assertEqual(volume_level(5, -10), 100)

    def test_invalid_settings_and_values(self):
        for value in (True, 1.2, -1, 101, "50"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                volume_db(value, -10)
        for value in (None, True, -61, 1, -10.5, "-10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings(max_volume_db=value)
        self.assertEqual(settings().max_volume_db, -10)
        entry = next(e for e in CONFIG.config_entries() if e.key == "max_volume_db")
        self.assertEqual(entry.default_value, -10)
        for value in (float("nan"), float("inf"), -97, 0.5, -40.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                volume_packet(value, 0)

    def test_header_sequence_crc_and_zero_padding(self):
        packet = volume_packet(-40, 511)
        self.assertEqual(len(packet), 142)
        self.assertEqual(packet[:8], bytes.fromhex("447200ff00ff0004"))
        self.assertEqual(int.from_bytes(packet[12:14], "big"), crc16(packet[:12]))
        self.assertEqual(packet[14:], bytes(128))
        self.assertEqual(volume_packet(0, 0)[8:10], b"\0\0")
        # Recorded from installed HA devialet_expert 0.1.4, without sending.
        for db, header in (
            (-96.5, "4472000000000004c2c100008941"),
            (-40, "4472000000000004c22000001e40"),
            (-10, "4472000000000004c1200000859c"),
            (0, "447200000000000400000000c64a"),
        ):
            self.assertEqual(volume_packet(db, 0), bytes.fromhex(header) + bytes(128))


class ControlFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.discovery = Discovery(port=0)
        await self.discovery.start()
        # Distinct timestamps even on Windows' coarse Python 3.12 clock.
        self.discovery.devices["192.0.2.20"] = status(received_at=time.monotonic() - 0.5)
        self.loop = asyncio.get_running_loop()
        self.send = AsyncMock()
        self.patch = patch.object(self.loop, "sock_sendto", self.send)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.discovery.close()


class VolumeSenderTests(ControlFixture):
    async def test_four_packets_use_existing_socket_and_new_confirmation(self):
        self.discovery._control_sequence = 510

        async def sent(sock, data, endpoint):
            self.assertIs(sock, self.discovery._socket)
            self.assertEqual(endpoint, ("192.0.2.20", 45455))
            self.discovery.devices[endpoint[0]] = status(volume_db=-10)

        self.send.side_effect = sent
        await self.discovery.set_volume("192.0.2.20", 100, -10)
        self.assertEqual(self.send.await_count, 4)
        self.assertEqual([call.args[1][3] for call in self.send.await_args_list], [254, 255, 0, 1])
        self.assertEqual(self.discovery._control_sequence, 2)

    async def test_stale_missing_off_and_invalid_never_send(self):
        for value in (
            None,
            status(received_at=time.monotonic() - 3),
            status(powered=False),
        ):
            self.discovery.devices.clear()
            if value is not None:
                self.discovery.devices[value.address] = value
            with self.assertRaises(AirError):
                await self.discovery.set_volume("192.0.2.20", 50, -10)
        self.send.assert_not_awaited()

    async def test_noop_and_bad_input_do_not_send(self):
        self.discovery.devices["192.0.2.20"] = status(volume_db=-10)
        await self.discovery.set_volume("192.0.2.20", 100, -10)
        with self.assertRaises(ValueError):
            await self.discovery.set_volume("192.0.2.20", 101, -10)
        self.send.assert_not_awaited()

    async def test_closed_receiver_refuses_control(self):
        await self.discovery.close()
        with self.assertRaisesRegex(AirError, "not been started"):
            await self.discovery.set_volume("192.0.2.20", 100, -10)
        self.send.assert_not_awaited()

    async def test_no_matching_broadcast_times_out_and_does_not_fake_state(self):
        before = self.discovery.devices["192.0.2.20"]
        with self.assertRaisesRegex(AirError, "confirming"):
            await self.discovery.set_volume("192.0.2.20", 100, -10, confirmation_seconds=0.01)
        self.assertIs(self.discovery.devices["192.0.2.20"], before)

    async def test_old_matching_status_cannot_confirm_new_command(self):
        before = self.discovery.devices["192.0.2.20"]

        async def sent(*args):
            self.discovery.devices[before.address] = replace(before, volume_db=-10)

        self.send.side_effect = sent
        with self.assertRaisesRegex(AirError, "confirming"):
            await self.discovery.set_volume("192.0.2.20", 100, -10, confirmation_seconds=0.01)

    async def test_send_error_and_cancel_release_lock(self):
        self.send.side_effect = OSError("offline")
        with self.assertRaisesRegex(AirError, "offline"):
            await self.discovery.set_volume("192.0.2.20", 100, -10)
        self.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.discovery.set_volume("192.0.2.20", 100, -10)
        self.assertFalse(self.discovery._control_lock.locked())


class PlayerVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mass = FakeMass()
        self.discovery = SimpleNamespace(devices={"192.0.2.20": status()})
        self.provider = SimpleNamespace(
            settings=settings(), mass=self.mass, discovery=self.discovery, logger=Mock()
        )
        self.player = PLAYER.DevialetAirPlayer(self.provider, "test_expert")

    async def test_volume_commands_are_confirmed_without_starting_audio(self):
        self.discovery.set_volume = AsyncMock()
        await self.player.volume_set(60)
        self.discovery.set_volume.assert_awaited_once_with("192.0.2.20", 60, -10)
        self.mass.streams.get_stream.assert_not_called()
        self.assertEqual(self.player._attr_volume_level, volume_level(-40, -10))
        self.discovery.devices["192.0.2.20"] = status(volume_db=-20)
        self.player.refresh_status()
        self.assertEqual(self.player._attr_volume_level, volume_level(-20, -10))

    async def test_volume_failure_surfaces_without_optimistic_change(self):
        self.player.refresh_status()
        before = self.player._attr_volume_level
        self.discovery.set_volume = AsyncMock(side_effect=AirError("not confirmed"))
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            await self.player.volume_set(100)
        self.assertEqual(self.player._attr_volume_level, before)
        self.assertIn("not confirmed", self.player._extra_attributes["air_control_error"])
        self.provider.logger.error.assert_called_once()


class PowerSenderTests(ControlFixture):
    async def test_power_wire_boolean_and_opcode(self):
        for powered in (False, True):
            packet = power_packet(powered, 511)
            self.assertEqual(len(packet), 142)
            self.assertEqual(packet[:8], b"Dr\0\xff\0\xff" + bytes((powered, 1)))
            self.assertEqual(int.from_bytes(packet[12:14], "big"), crc16(packet[:12]))
            self.assertEqual(packet[14:], bytes(128))
            header = "447200000000010100000000a0bd" if powered else "447200000000000100000000e51d"
            self.assertEqual(power_packet(powered, 0), bytes.fromhex(header) + bytes(128))
        for invalid in (0, 1, None, "on"):
            with self.assertRaises(ValueError):
                power_packet(invalid, 0)
            with self.assertRaises(ValueError):
                await self.discovery.set_power("192.0.2.20", invalid)
        self.send.assert_not_awaited()

    async def test_wake_waits_through_status_gap_for_fresh_on(self):
        before = status(powered=False)
        self.discovery.devices[before.address] = before

        async def sent(*args):
            # Simulate broadcasts disappearing while booting. A matching but
            # old ON status must not confirm the wake.
            self.discovery.devices[before.address] = replace(before, powered=True, received_at=0)

        async def boot():
            await asyncio.sleep(0.08)
            self.discovery.devices[before.address] = status(powered=True)

        self.send.side_effect = sent
        task = asyncio.create_task(boot())
        try:
            await self.discovery.set_power(before.address, True, confirmation_seconds=1)
        finally:
            await task
        self.assertEqual(self.send.await_count, 4)
        self.assertTrue(self.discovery.devices[before.address].powered)

    async def test_wake_noop_missing_stale_timeout_and_cancel(self):
        address = "192.0.2.20"
        await self.discovery.set_power(address, True)
        self.send.assert_not_awaited()
        self.discovery.devices[address] = status(received_at=0, powered=False)
        with self.assertRaises(AirError):
            await self.discovery.set_power(address, True)
        self.send.assert_not_awaited()
        self.discovery.devices.clear()
        with self.assertRaises(AirError):
            await self.discovery.set_power(address, True)
        self.discovery.devices[address] = status(powered=False)
        with self.assertRaisesRegex(AirError, "confirming Expert power"):
            await self.discovery.set_power(address, True, confirmation_seconds=0.01)
        self.assertFalse(self.discovery.devices[address].powered)
        self.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.discovery.set_power(address, True)
        self.assertFalse(self.discovery._control_lock.locked())

    async def test_standby_confirmation_and_shared_sequence(self):
        async def sent(sock, packet, endpoint):
            self.assertIs(sock, self.discovery._socket)
            self.assertEqual(packet[6:8], b"\0\1")
            self.discovery.devices[endpoint[0]] = status(powered=False)

        self.send.side_effect = sent
        await self.discovery.set_power("192.0.2.20", False)
        self.assertFalse(self.discovery.devices["192.0.2.20"].powered)
        self.assertEqual(self.discovery._control_sequence, 4)


class PlayerPowerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mass = FakeMass()
        self.discovery = SimpleNamespace(
            devices={"192.0.2.20": status(powered=False)},
            set_power=AsyncMock(),
            wait_for_ready=AsyncMock(return_value=status()),
        )
        self.provider = SimpleNamespace(
            settings=settings(),
            mass=self.mass,
            discovery=self.discovery,
            logger=Mock(),
            signal_provider_event=Mock(),
        )
        self.player = PLAYER.DevialetAirPlayer(self.provider, "test_expert")
        self.provider.player = self.player

    async def asyncTearDown(self):
        await self.player.stop()

    async def test_explicit_power_confirms_without_decoding_or_volume(self):
        async def wake(*args):
            self.discovery.devices["192.0.2.20"] = status()

        self.discovery.set_power.side_effect = wake
        await self.player.power(True)
        self.discovery.set_power.assert_awaited_once_with("192.0.2.20", True)
        self.assertTrue(self.player._attr_powered)
        self.mass.streams.get_stream.assert_not_called()

    async def test_failed_power_surfaces_and_does_not_fake_on(self):
        self.player.refresh_status()
        self.discovery.set_power.side_effect = AirError("wake not confirmed")
        with self.assertRaisesRegex(RuntimeError, "wake not confirmed"):
            await self.player.power(True)
        self.assertFalse(self.player._attr_powered)
        self.assertIn("wake not confirmed", self.player._extra_attributes["air_control_error"])

    async def test_play_wake_failure_never_opens_audio(self):
        self.discovery.set_power.side_effect = AirError("wake failed")
        with patch.object(PLAYER, "AirClient") as client:
            with self.assertRaisesRegex(RuntimeError, "wake failed"):
                await self.player.play_media(SimpleNamespace(source_id="queue"))
        client.assert_not_called()
        self.mass.streams.get_stream.assert_not_called()
        self.discovery.wait_for_ready.assert_not_awaited()

    async def test_play_confirms_wake_then_checks_input_before_audio(self):
        from devialet_air import PlaybackStats

        events = []

        async def wake(*args):
            events.append("wake")
            self.discovery.devices["192.0.2.20"] = status()

        async def ready(*args, **kwargs):
            events.append("ready")
            self.assertEqual(events, ["wake", "ready"])
            return status()

        async def play(source, **kwargs):
            events.append("audio")
            kwargs["on_started"]()
            async for _ in source:
                pass
            return PlaybackStats(source_frames=200, sent_blocks=1, sample_rate=48000)

        self.discovery.set_power.side_effect = wake
        self.discovery.wait_for_ready.side_effect = ready
        with patch.object(PLAYER, "AirClient", return_value=SimpleNamespace(play=play)):
            await self.player.play_media(SimpleNamespace(source_id="queue"))
            await asyncio.gather(*self.mass.tasks)
        self.assertEqual(events, ["wake", "ready", "audio"])

    async def test_wake_does_not_bypass_wrong_input_or_mute(self):
        self.discovery.wait_for_ready.side_effect = AirError("Select the network input")
        with patch.object(PLAYER, "AirClient") as client:
            with self.assertRaisesRegex(RuntimeError, "network input"):
                await self.player.play_media(SimpleNamespace(source_id="queue"))
        self.discovery.set_power.assert_awaited_once_with("192.0.2.20", True)
        client.assert_not_called()
        self.mass.streams.get_stream.assert_not_called()

    async def test_stop_cancels_wake_before_any_audio(self):
        pending = asyncio.Event()

        async def wake(*args):
            pending.set()
            await asyncio.Event().wait()

        self.discovery.set_power.side_effect = wake
        play = asyncio.create_task(self.player.play_media(SimpleNamespace(source_id="queue")))
        await pending.wait()
        await self.player.stop()
        with self.assertRaises(asyncio.CancelledError):
            await play
        self.mass.streams.get_stream.assert_not_called()
        self.assertIsNone(self.player._play_task)

    async def test_status_and_stop_never_wake(self):
        self.player.refresh_status()
        await self.player.stop()
        self.discovery.set_power.assert_not_awaited()
