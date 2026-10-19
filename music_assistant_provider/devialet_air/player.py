"""Bounded PCM playback with explicit startup, failure and cancellation semantics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING

from music_assistant.models.player import Player
from music_assistant_models.enums import ContentType, IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo, PlayerMedia

from devialet_air import AirClient, AirError, PlaybackStats
from devialet_air.control import volume_level

if TYPE_CHECKING:
    from .provider import DevialetAirProvider

STATUS_MAX_AGE = 2.0
MAX_SOURCE_CHUNK = 1024 * 1024
PCM_SLICE_BYTES = 19200
SESSION_QUIET_SECONDS = 3.0


async def pcm_slices(source: AsyncGenerator[bytes, None]) -> AsyncGenerator[bytes, None]:
    """Pull only when consumed; retain at most one bounded MA chunk and one slice."""
    async with aclosing(source):
        async for chunk in source:
            if len(chunk) > MAX_SOURCE_CHUNK:
                raise AirError("MA PCM chunk exceeds the 1 MiB provider staging limit.")
            for offset in range(0, len(chunk), PCM_SLICE_BYTES):
                yield chunk[offset : offset + PCM_SLICE_BYTES]


class DevialetAirPlayer(Player):
    """An Expert with AIR playback, hardware volume and confirmed wake."""

    def __init__(self, provider: DevialetAirProvider, player_id: str) -> None:
        super().__init__(provider, player_id)
        self.air_provider = provider
        self._attr_name = f"Devialet Expert ({provider.settings.host})"
        self._attr_available = False
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.POWER,
        }
        self._attr_supported_sample_rates = [(provider.settings.sample_rate, 16)]
        self._attr_device_info = DeviceInfo(
            model="Expert (experimental AIR)",
            manufacturer="Devialet",
            identifiers={IdentifierType.IP_ADDRESS: provider.settings.host},
        )
        self._play_task: asyncio.Task[None] | None = None
        self._command_lock = asyncio.Lock()
        self._abort_reason: str | None = None
        self._started = False
        self._session_closed_at: float | None = None

    async def volume_set(self, volume_level: int) -> None:
        """Change hardware volume only on an explicit MA volume command."""
        try:
            settings = self.air_provider.settings
            await self.air_provider.discovery.set_volume(
                settings.host, volume_level, settings.max_volume_db
            )
        except (AirError, ValueError) as error:
            message = f"Expert volume command failed: {error}"
            self.air_provider.logger.error(message)
            self._extra_attributes["air_control_error"] = message
            self.update_state()
            raise PlayerCommandFailed(message) from error
        self._extra_attributes.pop("air_control_error", None)
        self.refresh_status()

    async def power(self, powered: bool) -> None:
        """Expose MA power controls without changing volume, mute or input."""
        async with self._command_lock:
            if powered is False:
                await self._stop()
            try:
                await self.air_provider.discovery.set_power(
                    self.air_provider.settings.host, powered
                )
            except (AirError, ValueError) as error:
                message = f"Expert power command failed: {error}"
                self.air_provider.logger.error(message)
                self._extra_attributes["air_control_error"] = message
                self.update_state()
                raise PlayerCommandFailed(message) from error
            self._extra_attributes.pop("air_control_error", None)
            self.refresh_status()

    async def play_media(self, media: PlayerMedia) -> None:
        """Replace playback and propagate startup errors to the MA command caller."""
        async with self._command_lock:
            await self._stop()
            self._abort_reason = None
            self._started = False
            self._extra_attributes.pop("air_error", None)
            self._attr_current_media = media
            self._attr_active_source = media.source_id
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
            started = asyncio.get_running_loop().create_future()
            task = self.mass.create_task(self._run(media, started))
            self._play_task = task
            task.add_done_callback(lambda _: started.cancel() if not started.done() else None)
        # Do not hold the command lock while readiness/startup waits: stop must interrupt it.
        try:
            await asyncio.shield(started)
        except asyncio.CancelledError:
            async with self._command_lock:
                if self._play_task is task:
                    await self._stop()
            raise

    async def stop(self) -> None:
        """Cancel and await all source/transport cleanup without changing hardware."""
        async with self._command_lock:
            await self._stop()

    async def on_unload(self) -> None:
        """Ensure disabled/unregistered players cannot retain a sender."""
        try:
            await self.stop()
        finally:
            if self.air_provider.player is self:
                self.air_provider.player = None
            await super().on_unload()

    def refresh_status(self) -> None:
        """Publish passive status and abort an active session if readiness is lost."""
        settings = self.air_provider.settings
        discovery = self.air_provider.discovery
        status = discovery.devices.get(settings.host)
        self._attr_available = (
            status is not None and 0 <= time.monotonic() - status.received_at <= STATUS_MAX_AGE
        )
        if status is not None:
            self._attr_name = status.name or self._attr_name
            self._attr_powered = status.powered
            self._attr_volume_muted = status.muted
            self._attr_volume_level = volume_level(status.volume_db, settings.max_volume_db)
            self._extra_attributes.update(
                air_input_index=status.input_index,
                air_input_labels=list(status.input_labels),
                air_input_enabled=list(status.input_enabled),
                air_hardware_volume_db=status.volume_db,
                air_max_volume_db=settings.max_volume_db,
            )
        if self._started and self._play_task is not None and not self._play_task.done():
            try:
                discovery.require_ready(settings.host, settings.network_input_index)
            except Exception as err:
                self._abort_reason = f"AIR readiness lost: {err}"
                self._play_task.cancel()
        self.update_state()

    async def _stop(self) -> None:
        task = self._play_task
        if task is not None:
            self._abort_reason = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._play_task is task:
                self._play_task = None
        self._started = False
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_active_source = None
        self.update_state()

    async def _run(self, media: PlayerMedia, started: asyncio.Future[None]) -> None:
        client: AirClient | None = None
        try:
            settings = self.air_provider.settings
            self._extra_attributes["air_startup_phase"] = "handoff"
            if self._session_closed_at is not None:
                # The Expert can keep acknowledging the old session after its
                # sender closes. Do not overlap fresh startup with that tail.
                remaining = SESSION_QUIET_SECONDS - (time.monotonic() - self._session_closed_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._extra_attributes["air_startup_phase"] = "readiness"
            # A user-requested play may wake network standby. No background
            # monitoring, provider load or stop operation wakes the amplifier.
            await self.air_provider.discovery.set_power(settings.host, True)
            status = await self.air_provider.discovery.wait_for_ready(
                settings.host, settings.network_input_index, timeout=4
            )
            client = AirClient(
                settings.host,
                local_address=(
                    None if settings.local_address == "0.0.0.0" else settings.local_address
                ),
                sample_rate=settings.sample_rate,
                gain=settings.gain,
            )
            pcm_format = AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=settings.sample_rate,
                bit_depth=16,
                channels=2,
            )
            source = self.mass.streams.get_stream(
                media, pcm_format, self.player_id, force_flow_mode=True
            )

            def on_started() -> None:
                self._started = True
                self._extra_attributes["air_startup_phase"] = "playing"
                self._attr_playback_state = PlaybackState.PLAYING
                self.update_state()
                if not started.done():
                    started.set_result(None)

            async with aclosing(source), aclosing(pcm_slices(source)) as pcm:
                self._extra_attributes["air_startup_phase"] = "transport"
                stats = await client.play(
                    pcm,
                    status=status,
                    network_input_index=settings.network_input_index,
                    status_provider=lambda: self.air_provider.discovery.require_ready(
                        settings.host, settings.network_input_index
                    ),
                    on_started=on_started,
                    on_progress=self._on_progress,
                )
            self._on_progress(stats)
            if not started.done():
                raise AirError("AIR ended before startup was accepted.")
        except asyncio.CancelledError:
            if self._abort_reason:
                self._report_error(self._abort_reason, started)
            elif not started.done():
                started.cancel()
            raise
        except Exception as err:
            self._report_error(f"AIR playback failed: {err}", started)
        finally:
            if client is not None:
                self._session_closed_at = time.monotonic()
            self._started = False
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self._attr_active_source = None
            if self._play_task is asyncio.current_task():
                self._play_task = None
            self.update_state()

    def _on_progress(self, stats: PlaybackStats) -> None:
        self._attr_elapsed_time = stats.elapsed_seconds
        self._attr_elapsed_time_last_updated = time.time()
        self._extra_attributes.update(
            air_source_frames=stats.source_frames,
            air_sent_blocks=stats.sent_blocks,
            air_acknowledged_frame=stats.acknowledged_frame,
            air_receiver_frame=stats.receiver_frame,
            air_resends=stats.resends,
            air_invalid_feedback=stats.invalid_feedback,
        )
        self.update_state()

    def _report_error(self, message: str, started: asyncio.Future[None]) -> None:
        # MA's task runner logs failures, but a background stream must also publish
        # an explicit player update; otherwise it looks like successful natural EOF.
        self.air_provider.logger.error("%s: %s", self.player_id, message)
        self._extra_attributes["air_error"] = message
        self.air_provider.signal_provider_event(
            {"type": "playback_error", "player_id": self.player_id, "message": message},
            sub_scope=self.player_id,
        )
        self.update_state(force_update=True)
        if not started.done():
            started.set_exception(PlayerCommandFailed(message))
