"""One-Expert provider lifecycle and passive status monitoring."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from music_assistant.models.player_provider import PlayerProvider
from music_assistant_models.config_entries import ConfigEntry

from devialet_air import Discovery, ExpertStatus

from .config import AirSettings, config_entries
from .player import DevialetAirPlayer


class DevialetAirProvider(PlayerProvider):
    """Manage one manually configured Expert and one passive discovery socket."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Expose the same settings used during setup."""
        entries = config_entries()
        for entry in entries:
            entry.default_value = self.get_setup_value(entry.key, entry.default_value)
            if entry.key == "gain" and type(entry.default_value) in (int, float):
                entry.default_value = f"{entry.default_value:g}"
        return entries

    async def handle_async_init(self) -> None:
        """Validate configuration before starting passive discovery."""
        self.settings = AirSettings.parse(
            {
                entry.key: self.config.get_value(entry.key, entry.default_value)
                for entry in config_entries()
            }
        )
        self.player: DevialetAirPlayer | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self.discovery = Discovery(self._on_status)
        try:
            await self.discovery.start()
        except BaseException:
            await self.discovery.close()
            raise

    async def loaded_in_mass(self) -> None:
        """Register the explicit player even when it is currently offline."""
        await self.discover_players()
        self._monitor_task = self.mass.create_task(self._monitor())

    async def discover_players(self) -> None:
        """Register only the manual host, never every observed broadcast."""
        player_id = f"devialet_air_{self.settings.host.replace('.', '_')}"
        if self.mass.players.get_player(player_id):
            return
        player = DevialetAirPlayer(self, player_id)
        await self.mass.players.register(player)
        self.player = player
        player.refresh_status()

    async def unload(self, is_removed: bool = False) -> None:
        """Release the session, player and discovery socket on every unload."""
        try:
            if task := getattr(self, "_monitor_task", None):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            self._monitor_task = None
            try:
                if player := getattr(self, "player", None):
                    try:
                        await player.stop()
                    finally:
                        await self.mass.players.unregister(player.player_id)
                        self.player = None
            finally:
                if discovery := getattr(self, "discovery", None):
                    await discovery.close()

    def _on_status(self, status: ExpertStatus) -> None:
        if status.address == self.settings.host and self.player is not None:
            self.player.refresh_status()

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.player is not None:
                self.player.refresh_status()
