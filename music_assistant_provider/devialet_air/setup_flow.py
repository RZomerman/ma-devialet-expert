"""Music Assistant's coroutine-based provider setup flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import AirSettings, config_entries

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect explicit endpoint/input choices without contacting the amplifier."""
    errors = None
    while True:
        values = await session.form(list(config_entries()), errors=errors, last_step=True)
        try:
            AirSettings.parse(values)
        except ValueError as err:
            errors = {"base": str(err)}
            continue
        await session.finish(values)
        return
