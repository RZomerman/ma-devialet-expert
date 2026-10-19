"""Offline provider lifecycle tests; MA test doubles never ship in the provider."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import struct
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from devialet_air import AirError, ExpertStatus, PlaybackStats
from tests.helpers import ExpertSimulator, decode_audio, ready
from tests.test_transport import client_for

ROOT = Path(__file__).resolve().parents[1]


class Entry(SimpleNamespace):
    """Minimal config hook with the pinned models' value/title order."""

    def __init__(self, value=None, title=None, default_value=None, **kwargs):
        super().__init__(value=value, title=title, default_value=default_value, **kwargs)


class FakePlayer:
    def __init__(self, provider, player_id):
        self.mass = provider.mass
        self.player_id = player_id
        self._extra_attributes = {}
        self._attr_playback_state = "idle"
        self.updates = []

    def update_state(self, **kwargs):
        self.updates.append(kwargs)

    async def on_unload(self):
        pass


class FakeProvider:
    def __init__(self, mass, manifest=None, config=None, features=None):
        self.mass = mass
        self.config = config
        self.logger = logging.getLogger("test.devialet_air")
        self.signal_provider_event = Mock()


def load_overlay():
    """Load the actual overlay, substituting only MA's unavailable runtime hooks."""
    modules = {}
    for name in (
        "music_assistant",
        "music_assistant.models",
        "music_assistant.models.player",
        "music_assistant.models.player_provider",
        "music_assistant_models",
        "music_assistant_models.config_entries",
        "music_assistant_models.enums",
        "music_assistant_models.errors",
        "music_assistant_models.media_items",
        "music_assistant_models.player",
    ):
        modules[name] = types.ModuleType(name)
    modules["music_assistant.models.player"].Player = FakePlayer
    modules["music_assistant.models.player_provider"].PlayerProvider = FakeProvider
    entries = modules["music_assistant_models.config_entries"]
    entries.ConfigEntry = Entry
    entries.ConfigValueOption = Entry
    enums = modules["music_assistant_models.enums"]
    enums.ConfigEntryType = SimpleNamespace(STRING="string", INTEGER="integer", FLOAT="float")
    enums.ProviderFeature = str
    enums.ContentType = SimpleNamespace(PCM_S16LE="pcm_s16le")
    enums.IdentifierType = SimpleNamespace(IP_ADDRESS="ip_address")
    enums.PlaybackState = SimpleNamespace(PLAYING="playing", IDLE="idle")
    enums.PlayerFeature = SimpleNamespace(
        PLAY_MEDIA="play_media", VOLUME_SET="volume_set", POWER="power"
    )
    modules["music_assistant_models.errors"].PlayerCommandFailed = RuntimeError
    modules["music_assistant_models.media_items"].AudioFormat = SimpleNamespace
    modules["music_assistant_models.player"].DeviceInfo = SimpleNamespace
    modules["music_assistant_models.player"].PlayerMedia = SimpleNamespace
    path = ROOT / "music_assistant_provider" / "devialet_air" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "_air_test_overlay", path, submodule_search_locations=[str(path.parent)]
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        __import__("_air_test_overlay.setup_flow")
        return (
            module,
            sys.modules["_air_test_overlay.config"],
            sys.modules["_air_test_overlay.player"],
            sys.modules["_air_test_overlay.provider"],
            sys.modules["_air_test_overlay.setup_flow"],
        )


OVERLAY, CONFIG, PLAYER, PROVIDER, FLOW = load_overlay()


def settings(**kwargs):
    return CONFIG.AirSettings.parse({"host": "192.0.2.20", "network_input_index": 1, **kwargs})


def status(**kwargs):
    values = dict(
        address="192.0.2.20",
        name="Expert",
        powered=True,
        muted=False,
        input_index=1,
        input_labels=("USB", "Wi-Fi"),
        input_enabled=(True, True),
        volume_db=-40,
        received_at=time.monotonic(),
    )
    return ExpertStatus(**(values | kwargs))


class FakeMass:
    def __init__(self):
        self.streams = SimpleNamespace(get_stream=Mock(side_effect=self.source))
        self.players = SimpleNamespace(
            register=AsyncMock(side_effect=self.register),
            unregister=AsyncMock(side_effect=self.unregister),
            get_player=lambda player_id: self.registered.get(player_id),
        )
        self.registered = {}
        self.closed_sources = 0
        self.source_started = 0
        self.tasks = []

    def create_task(self, target):
        task = asyncio.create_task(target)
        self.tasks.append(task)
        return task

    async def register(self, player):
        self.registered[player.player_id] = player

    async def unregister(self, player_id):
        player = self.registered.pop(player_id, None)
        if player:
            await player.on_unload()

    async def source(self, *args, **kwargs):
        self.source_started += 1
        try:
            yield b"\0" * 800
        finally:
            self.closed_sources += 1


class ConfigTests(unittest.TestCase):
    def test_defaults_and_explicit_input(self):
        self.assertEqual(settings().sample_rate, 48000)
        self.assertEqual(settings().gain, 0.25)
        self.assertEqual(settings(sample_rate=44100).sample_rate, 44100)
        with self.assertRaises(ValueError):
            CONFIG.AirSettings.parse({"host": "192.0.2.20"})

    def test_ipv4_only_unicast(self):
        for host in ("localhost", "::1", "0.0.0.0", "224.0.0.1", "255.255.255.255", 123):
            with self.subTest(host=host), self.assertRaises(ValueError):
                settings(host=host)
        self.assertEqual(settings(host=" 192.0.2.20 ").host, "192.0.2.20")
        with self.assertRaises(ValueError):
            settings(local_address="bad")

    def test_gain_rate_and_input_bounds(self):
        for gain in (float("nan"), float("inf"), -0.01, 0, 0.0, 1.01, True, "invalid"):
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                settings(gain=gain)
        for rate in (96000, True, "48000"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                settings(sample_rate=rate)
        for index in (-1, 15, 256, True, "1"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                settings(network_input_index=index)
        self.assertEqual(settings(gain=0.001).gain, 0.001)
        self.assertEqual(settings(gain=1).gain, 1)
        gain_entry = next(entry for entry in CONFIG.config_entries() if entry.key == "gain")
        self.assertFalse(gain_entry.validate(0))
        self.assertTrue(gain_entry.validate(0.25))

    def test_options_use_actual_value_title_order(self):
        rate = next(entry for entry in CONFIG.config_entries() if entry.key == "sample_rate")
        self.assertEqual([option.value for option in rate.options], [48000, 44100])
        gain = next(entry for entry in CONFIG.config_entries() if entry.key == "gain")
        self.assertEqual(
            [option.value for option in gain.options], ["0.05", "0.1", "0.25", "0.5", "0.8", "1"]
        )
        self.assertEqual(settings(gain="0.1").gain, 0.1)
        for value in ("invalid", "nan", "inf", "0", "-1", "1.1"):
            with self.subTest(gain=value), self.assertRaises(ValueError):
                settings(gain=value)
        self.assertIsNone(
            next(
                entry for entry in CONFIG.config_entries() if entry.key == "network_input_index"
            ).default_value,
        )


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mass = FakeMass()
        self.discovery = SimpleNamespace(
            devices={"192.0.2.20": status()},
            wait_for_ready=AsyncMock(return_value=status()),
            set_power=AsyncMock(),
            require_ready=Mock(return_value=status()),
            close=AsyncMock(),
        )
        self.provider = SimpleNamespace(
            settings=settings(),
            mass=self.mass,
            discovery=self.discovery,
            logger=Mock(),
            signal_provider_event=Mock(),
            player=None,
        )
        self.player = PLAYER.DevialetAirPlayer(self.provider, "devialet_air_192_0_2_20")
        self.provider.player = self.player
        self.media = SimpleNamespace(source_id="queue", uri="unused")

    async def asyncTearDown(self):
        await self.player.stop()
        for task in self.mass.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.mass.tasks, return_exceptions=True)

    def client(self, play):
        return patch.object(PLAYER, "AirClient", return_value=SimpleNamespace(play=play))

    async def test_stream_format_eof_and_cleanup(self):
        received = []

        async def play(source, **kwargs):
            kwargs["status_provider"]()
            kwargs["on_started"]()
            async for chunk in source:
                received.append(chunk)
            return PlaybackStats(source_frames=200, sent_blocks=1, sample_rate=48000)

        with self.client(play) as factory:
            await self.player.play_media(self.media)
            await asyncio.gather(*self.mass.tasks)
        self.assertEqual(sum(map(len, received)), 800)
        factory.assert_called_once_with(
            "192.0.2.20", local_address=None, sample_rate=48000, gain=0.25
        )
        args, kwargs = self.mass.streams.get_stream.call_args
        self.assertTrue(kwargs["force_flow_mode"])
        self.assertEqual(
            (args[1].content_type, args[1].channels, args[1].bit_depth), ("pcm_s16le", 2, 16)
        )
        self.assertEqual(args[2], self.player.player_id)
        self.assertEqual(self.player._attr_playback_state, "idle")
        self.assertIsNone(self.player._attr_current_media)
        self.assertEqual(self.mass.closed_sources, 1)
        self.assertNotIn("air_error", self.player._extra_attributes)
        self.assertEqual(
            self.player._attr_supported_features, {"play_media", "volume_set", "power"}
        )
        self.discovery.require_ready.assert_called_once_with("192.0.2.20", 1)

    async def test_slow_source_rechecks_readiness_before_startup(self):
        first_pcm = asyncio.Event()
        allow_pcm = asyncio.Event()

        async def source(*args, **kwargs):
            first_pcm.set()
            await allow_pcm.wait()
            yield b"\0" * 800

        async def play(source, **kwargs):
            await anext(source)
            kwargs["status_provider"]()
            self.fail("Startup must not be attempted after source preflight loses readiness")

        self.mass.streams.get_stream.side_effect = source
        with self.client(play):
            command = asyncio.create_task(self.player.play_media(self.media))
            await first_pcm.wait()
            self.discovery.require_ready.side_effect = AirError("stale after source lookup")
            allow_pcm.set()
            with self.assertRaisesRegex(RuntimeError, "stale after source lookup"):
                await command
        self.assertFalse(self.player._started)
        self.assertEqual(self.player._attr_playback_state, "idle")

    async def test_startup_failure_is_command_error_and_player_notification(self):
        async def play(source, **kwargs):
            raise AirError("startup rejected")

        with self.client(play), self.assertRaisesRegex(RuntimeError, "startup rejected"):
            await self.player.play_media(self.media)
        self.assertEqual(self.player._attr_playback_state, "idle")
        self.assertIn("startup rejected", self.player._extra_attributes["air_error"])
        self.provider.logger.error.assert_called_once()
        self.provider.signal_provider_event.assert_called_once_with(
            {
                "type": "playback_error",
                "player_id": self.player.player_id,
                "message": "AIR playback failed: startup rejected",
            },
            sub_scope=self.player.player_id,
        )
        self.assertIn({"force_update": True}, self.player.updates)

    async def test_readiness_timeout_never_opens_source_or_sender(self):
        self.discovery.wait_for_ready.side_effect = TimeoutError("no Expert")
        with patch.object(PLAYER, "AirClient") as factory:
            with self.assertRaisesRegex(RuntimeError, "no Expert"):
                await self.player.play_media(self.media)
            factory.assert_not_called()
        self.mass.streams.get_stream.assert_not_called()

    async def test_runtime_failure_is_not_silent_eof(self):
        fail = asyncio.Event()

        async def play(source, **kwargs):
            kwargs["on_started"]()
            async for _ in source:
                await fail.wait()
                raise AirError("feedback watchdog")

        with self.client(play):
            await self.player.play_media(self.media)
            task = self.player._play_task
            fail.set()
            await task
        self.assertIn("feedback watchdog", self.player._extra_attributes["air_error"])
        self.assertEqual(self.mass.closed_sources, 1)
        self.assertEqual(self.player._attr_playback_state, "idle")

    async def test_stop_cancels_blocked_source_and_waits_for_cleanup(self):
        cleaned = asyncio.Event()

        async def play(source, **kwargs):
            kwargs["on_started"]()
            try:
                async for _ in source:
                    await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with self.client(play):
            await self.player.play_media(self.media)
            await self.player.stop()
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.mass.closed_sources, 1)
        self.assertIsNone(self.player._play_task)
        self.assertNotIn("air_error", self.player._extra_attributes)

    async def test_next_closes_old_source_and_waits_before_replacement(self):
        events = []
        sends = 0
        closed_at = []

        async def play(source, **kwargs):
            nonlocal sends
            sends += 1
            if sends == 2:
                self.assertEqual(events, ["start", "closed"])
                self.assertEqual(self.mass.closed_sources, 1)
                self.assertGreaterEqual(time.monotonic() - closed_at[0], 0.05)
            events.append("start")
            kwargs["on_started"]()
            try:
                async for _ in source:
                    await asyncio.Event().wait()
            finally:
                events.append("closed")
                closed_at.append(time.monotonic())

        with self.client(play), patch.object(PLAYER, "SESSION_QUIET_SECONDS", 0.06):
            await self.player.play_media(self.media)
            await asyncio.wait_for(self.player.play_media(self.media), 1)
            self.assertEqual(self.player._attr_playback_state, "playing")
            self.assertNotIn("air_error", self.player._extra_attributes)
            await self.player.stop()
        self.assertEqual(events, ["start", "closed", "start", "closed"])
        self.assertEqual(self.mass.closed_sources, 2)

    async def test_stop_during_handoff_cancels_without_starting_new_sender(self):
        self.player._session_closed_at = time.monotonic()
        with patch.object(PLAYER, "AirClient") as client:
            command = asyncio.create_task(self.player.play_media(self.media))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.wait_for(self.player.stop(), 0.5)
            with self.assertRaises(asyncio.CancelledError):
                await command
        client.assert_not_called()
        self.mass.streams.get_stream.assert_not_called()
        self.discovery.set_power.assert_not_awaited()

    async def test_switch_reuses_udp_port_only_after_old_transport_closes(self):
        self.provider.settings = settings(host="127.0.0.1", network_input_index=9)
        self.discovery.wait_for_ready.side_effect = lambda *a, **kw: ready()
        self.discovery.require_ready.side_effect = lambda *a, **kw: ready()
        closures = []

        async def source(*args, **kwargs):
            try:
                while True:
                    yield b"\0" * 800
            finally:
                closures.append(time.monotonic())

        self.mass.streams.get_stream.side_effect = source
        async with ExpertSimulator() as first, ExpertSimulator() as second:
            old_client = client_for(first)
            new_client = client_for(second)
            with patch.object(PLAYER, "AirClient", return_value=old_client):
                await self.player.play_media(self.media)
                await asyncio.wait_for(first.audio_started.wait(), 1)
                port = old_client._socket.getsockname()[1]
            new_client._feedback_port = port

            def replacement(*args, **kwargs):
                self.assertIsNone(old_client._socket)
                self.assertFalse(old_client._playing)
                self.assertEqual(len(closures), 1)
                self.assertGreaterEqual(time.monotonic() - closures[0], 0.05)
                return new_client

            with (
                patch.object(PLAYER, "AirClient", side_effect=replacement),
                patch.object(PLAYER, "SESSION_QUIET_SECONDS", 0.06),
            ):
                await asyncio.wait_for(self.player.play_media(self.media), 2)
                await asyncio.wait_for(second.audio_started.wait(), 1)
                self.assertEqual(new_client._socket.getsockname()[1], port)
                self.assertEqual(self.player._attr_playback_state, "playing")
                self.assertNotIn("air_error", self.player._extra_attributes)
                await self.player.stop()
            self.assertIsNone(new_client._socket)
        self.assertEqual(len(closures), 2)
        self.assertTrue(all(task.done() for task in self.mass.tasks))

    async def test_expired_handoff_does_not_add_startup_delay(self):
        self.player._session_closed_at = time.monotonic() - 10

        async def play(source, **kwargs):
            kwargs["on_started"]()
            await asyncio.Event().wait()

        with self.client(play):
            await asyncio.wait_for(self.player.play_media(self.media), 0.5)
            await self.player.stop()

    async def test_stop_while_readiness_waits(self):
        async def wait(*args, **kwargs):
            await asyncio.Event().wait()

        self.discovery.wait_for_ready.side_effect = wait
        command = asyncio.create_task(self.player.play_media(self.media))
        await asyncio.sleep(0)
        await self.player.stop()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.mass.streams.get_stream.assert_not_called()

    async def test_cancel_command_cancels_transport(self):
        self.discovery.wait_for_ready.side_effect = lambda *a, **kw: None

        async def wait(*args, **kwargs):
            await asyncio.Event().wait()

        self.discovery.wait_for_ready.side_effect = wait
        command = asyncio.create_task(self.player.play_media(self.media))
        await asyncio.sleep(0)
        command.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.assertIsNone(self.player._play_task)

    async def test_readiness_loss_aborts_with_error(self):
        async def play(source, **kwargs):
            kwargs["on_started"]()
            await asyncio.Event().wait()

        with self.client(play):
            await self.player.play_media(self.media)
            task = self.player._play_task
            self.discovery.require_ready.side_effect = AirError("muted")
            self.player.refresh_status()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn("readiness lost", self.player._extra_attributes["air_error"])

    async def test_status_staleness_and_manual_identity(self):
        self.player.refresh_status()
        self.assertTrue(self.player._attr_available)
        self.assertEqual(self.player._attr_powered, True)
        self.discovery.devices["192.0.2.20"] = status(received_at=time.monotonic() - 60)
        self.player.refresh_status()
        self.assertFalse(self.player._attr_available)
        self.discovery.devices = {"192.0.2.21": status(address="192.0.2.21")}
        self.player.refresh_status()
        self.assertFalse(self.player._attr_available)

    async def test_setup_reprompts_then_finishes(self):
        session = SimpleNamespace(
            form=AsyncMock(
                side_effect=[
                    {"host": "invalid", "network_input_index": 1},
                    {"host": "192.0.2.20", "network_input_index": 1},
                ]
            ),
            finish=AsyncMock(),
        )
        await FLOW.run_setup(session)
        self.assertEqual(session.form.await_count, 2)
        session.finish.assert_awaited_once_with({"host": "192.0.2.20", "network_input_index": 1})

    async def test_registration_is_one_stable_manual_player_and_unload_closes(self):
        config = SimpleNamespace(
            get_value=lambda key, default=None: {
                "host": "192.0.2.20",
                "network_input_index": 1,
                "sample_rate": 48000,
                "gain": 0.25,
                "local_address": "0.0.0.0",
            }.get(key, default)
        )
        discovery = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            devices={},
        )
        provider = PROVIDER.DevialetAirProvider(self.mass, config=config)
        with patch.object(PROVIDER, "Discovery", return_value=discovery):
            await provider.handle_async_init()
            await provider.loaded_in_mass()
            await provider.discover_players()
            self.assertEqual(list(self.mass.registered), ["devialet_air_192_0_2_20"])
            self.assertFalse(provider.player._attr_available)
            await provider.unload()
        discovery.close.assert_awaited_once()
        self.assertEqual(self.mass.registered, {})
        self.assertTrue(all(task.done() for task in self.mass.tasks))

    async def test_options_default_to_persisted_setup_answers(self):
        answers = {
            "host": "192.0.2.20",
            "network_input_index": 9,
            "sample_rate": 44100,
            "gain": 0.125,
            "local_address": "192.0.2.10",
        }
        provider = PROVIDER.DevialetAirProvider(self.mass)
        provider.get_setup_value = Mock(side_effect=lambda key, default: answers.get(key, default))
        entries = await provider.get_config_entries()
        self.assertEqual(
            {entry.key: entry.default_value for entry in entries},
            {**answers, "gain": "0.125", "max_volume_db": -10},
        )
        self.assertIsNone(CONFIG.config_entries()[0].default_value)

    async def test_failed_monitor_still_closes_owned_resources(self):
        provider = PROVIDER.DevialetAirProvider(self.mass)
        provider.player = self.player
        provider.discovery = self.discovery

        async def failed():
            raise RuntimeError("monitor failed")

        provider._monitor_task = asyncio.create_task(failed())
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "monitor failed"):
            await provider.unload()
        self.discovery.close.assert_awaited_once()
        self.mass.players.unregister.assert_awaited_once_with(self.player.player_id)

    async def test_real_air_client_loopback_exact_tail_eof_and_cleanup(self):
        pcm = b"".join(struct.pack("<hh", i - 200, 200 - i) for i in range(409))
        self.provider.settings = settings(host="127.0.0.1", network_input_index=9)
        self.discovery.wait_for_ready.side_effect = lambda *args, **kwargs: ready()
        self.discovery.require_ready.side_effect = lambda *args, **kwargs: ready()
        self.player = PLAYER.DevialetAirPlayer(self.provider, "devialet_air_127_0_0_1")
        self.provider.player = self.player

        async def source(*args, **kwargs):
            try:
                for offset in range(0, len(pcm), 333):
                    yield pcm[offset : offset + 333]
            finally:
                self.mass.closed_sources += 1

        self.mass.streams.get_stream.side_effect = source
        async with ExpertSimulator() as sim:
            client = client_for(sim)
            with patch.object(PLAYER, "AirClient", return_value=client):
                await asyncio.wait_for(self.player.play_media(self.media), 2)
                await asyncio.wait_for(asyncio.gather(*self.mass.tasks), 2)
            self.discovery.require_ready.assert_called_once_with("127.0.0.1", 9)
            self.assertEqual(self.player._extra_attributes["air_source_frames"], 409)
            self.assertEqual(self.player._extra_attributes["air_sent_blocks"], 3)
            self.assertEqual(self.player._extra_attributes["air_acknowledged_frame"], 400)
            self.assertEqual(self.player._extra_attributes["air_receiver_frame"], 600)
            self.assertEqual(sorted(sim.packets), [0, 200, 400])
            for block, packet in sorted(sim.packets.items()):
                expected = [
                    (
                        round((i - 200) * 0.25 * 77.276),
                        round((200 - i) * 0.25 * 77.276),
                    )
                    if i < 409
                    else (0, 0)
                    for i in range(block, block + 200)
                ]
                self.assertEqual(list(zip(*decode_audio(packet), strict=True)), expected)
            self.assertFalse(client._playing)
            self.assertIsNone(client._socket)
        self.assertEqual(self.mass.closed_sources, 1)
        self.assertIsNone(self.player._play_task)
        self.assertEqual(self.player._attr_playback_state, "idle")
        self.assertIsNone(self.player._attr_current_media)
        self.assertNotIn("air_error", self.player._extra_attributes)
        self.assertTrue(all(task.done() for task in self.mass.tasks))

    @unittest.skipUnless(
        os.environ.get("DEVIALET_AIR_SOAK_SECONDS"),
        "Opt-in sustained UDP test; set DEVIALET_AIR_SOAK_SECONDS=15",
    )
    async def test_sustained_provider_udp_with_periodic_lost_resends(self):
        seconds = int(os.environ["DEVIALET_AIR_SOAK_SECONDS"])
        self.assertGreaterEqual(seconds, 10)
        self.assertLessEqual(seconds, 120)
        pattern = b"".join(struct.pack("<hh", i - 100, 100 - i) for i in range(200))
        expected_left = [round((i - 100) * 0.25 * 77.276) for i in range(200)]
        expected_right = [round((100 - i) * 0.25 * 77.276) for i in range(200)]
        for rate in (48000, 44100):
            with self.subTest(rate=rate):
                frames = rate * seconds + 109
                blocks = (frames + 199) // 200
                self.provider.settings = settings(
                    host="127.0.0.1", network_input_index=9, sample_rate=rate
                )
                self.discovery.wait_for_ready.side_effect = lambda *args, **kwargs: ready()
                self.discovery.require_ready.side_effect = lambda *args, **kwargs: ready()
                self.player = PLAYER.DevialetAirPlayer(self.provider, "devialet_air_127_0_0_1")
                self.provider.player = self.player
                closed_before = self.mass.closed_sources

                async def source(*args, frames=frames, **kwargs):
                    remaining = frames * 4
                    try:
                        while remaining:
                            chunk = (pattern * 32)[:remaining]
                            remaining -= len(chunk)
                            yield chunk
                    finally:
                        self.mass.closed_sources += 1

                self.mass.streams.get_stream.side_effect = source
                async with ExpertSimulator("periodic_repair") as sim:
                    client = client_for(sim, rate=rate)
                    start = time.perf_counter()
                    with patch.object(PLAYER, "AirClient", return_value=client):
                        await asyncio.wait_for(self.player.play_media(self.media), 3)
                        await asyncio.wait_for(asyncio.gather(*self.mass.tasks), seconds + 5)
                    elapsed = time.perf_counter() - start
                    self.assertNotIn("air_error", self.player._extra_attributes)
                    self.assertGreaterEqual(elapsed, seconds * 0.95)
                    self.assertLessEqual(elapsed, seconds + 2)
                    self.assertGreaterEqual(client.stats.elapsed_seconds, seconds - 0.05)
                    self.assertLessEqual(client.stats.elapsed_seconds, seconds + 0.1)
                    self.assertEqual(client.stats.source_frames, frames)
                    self.assertEqual(client.stats.sent_blocks, blocks)
                    self.assertEqual(client.stats.acknowledged_frame, (blocks - 1) * 200)
                    self.assertGreaterEqual(client.stats.receiver_frame, blocks * 200)
                    self.assertEqual(len(sim.packets), blocks)
                    drops = (blocks - 1) // 500
                    self.assertGreaterEqual(client.stats.resends, drops * 2)
                    for index in range(500, blocks, 500):
                        self.assertGreaterEqual(sim.attempts[index * 200], 3)
                    for counter, packet in sim.packets.items():
                        left, right = decode_audio(packet)
                        length = min(200, frames - counter)
                        self.assertEqual(left, expected_left[:length] + [0] * (200 - length))
                        self.assertEqual(right, expected_right[:length] + [0] * (200 - length))
                    self.assertLessEqual(len(client._cache), 400)
                    self.assertFalse(client._playing)
                    self.assertIsNone(client._socket)
                    print(
                        "UDP_SOAK "
                        + json.dumps(
                            {
                                "sample_rate": rate,
                                "source_frames": frames,
                                "unique_blocks": blocks,
                                "resends": client.stats.resends,
                                "periodic_double_drops": drops,
                                "elapsed_seconds": round(elapsed, 3),
                                "max_send_lateness_ms": round(
                                    client.stats.max_lateness_seconds * 1000, 3
                                ),
                                "scope": "real provider/core, simulated MA hooks and Expert",
                            }
                        )
                    )
                self.assertEqual(self.mass.closed_sources, closed_before + 1)
                self.assertIsNone(self.player._play_task)
                self.assertEqual(self.player._attr_playback_state, "idle")
                self.assertTrue(all(task.done() for task in self.mass.tasks))


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slicing_preserves_bytes_short_reads_and_closes(self):
        closed = False
        expected = [b"\x01", b"\x02" * 40000, b"", b"\x03" * 11]

        async def source():
            nonlocal closed
            try:
                for chunk in expected:
                    yield chunk
            finally:
                closed = True

        chunks = [chunk async for chunk in PLAYER.pcm_slices(source())]
        self.assertEqual(b"".join(chunks), b"".join(expected))
        self.assertLessEqual(max(map(len, chunks)), PLAYER.PCM_SLICE_BYTES)
        self.assertTrue(closed)

    async def test_oversized_chunk_fails_and_closes(self):
        closed = False

        async def source():
            nonlocal closed
            try:
                yield b"\0" * (PLAYER.MAX_SOURCE_CHUNK + 1)
            finally:
                closed = True

        with self.assertRaises(AirError):
            async for _ in PLAYER.pcm_slices(source()):
                self.fail("oversized chunk was yielded")
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
