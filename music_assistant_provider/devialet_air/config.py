"""Validated, deliberately narrow AIR provider settings."""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType


@dataclass(frozen=True)
class AirSettings:
    """Settings for one explicit IPv4 endpoint and network input."""

    host: str
    network_input_index: int
    sample_rate: int = 48000
    gain: float = 0.25
    local_address: str = "0.0.0.0"
    max_volume_db: int = -10

    @classmethod
    def parse(cls, values: Mapping[str, Any]) -> AirSettings:
        """Reject unsafe or unsupported settings before opening sockets."""
        host = _ipv4(values.get("host"), allow_unspecified=False)
        local_address = _ipv4(values.get("local_address", "0.0.0.0"), allow_unspecified=True)
        index = values.get("network_input_index")
        rate = values.get("sample_rate", 48000)
        gain = values.get("gain", 0.25)
        maximum = values.get("max_volume_db", -10)
        if type(maximum) is not int or not -60 <= maximum <= 0:
            raise ValueError("Maximum hardware volume must be an integer from -60 to 0 dB.")
        if type(index) is not int or not 0 <= index <= 14:
            raise ValueError("Select the Expert's zero-based network input index (0..14).")
        if type(rate) is not int or rate not in (44100, 48000):
            raise ValueError("Sample rate must be 48000 or experimental 44100.")
        if not _valid_gain(gain):
            raise ValueError("Software gain must be finite, greater than 0 and at most 1.")
        return cls(host, index, rate, float(gain), local_address, maximum)


def _valid_gain(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return False
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and 0 < value <= 1
        and math.isfinite(value)
    )


def _ipv4(value: Any, *, allow_unspecified: bool) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip() if isinstance(value, str) else "")
    except ipaddress.AddressValueError as err:
        raise ValueError("Use an explicit IPv4 address, not a hostname or IPv6 address.") from err
    if address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError("Use a unicast IPv4 address.")
    if address.is_unspecified and not allow_unspecified:
        raise ValueError("The Expert address cannot be 0.0.0.0.")
    return str(address)


def config_entries() -> tuple[ConfigEntry, ...]:
    """Return matching setup and options fields."""
    return (
        ConfigEntry(
            key="host", type=ConfigEntryType.STRING, label="Expert IPv4 address", required=True
        ),
        ConfigEntry(
            key="network_input_index",
            type=ConfigEntryType.INTEGER,
            label="Network input index (zero-based)",
            required=True,
            range=(0, 14),
            description="Explicitly identify the configured Ethernet/Wi-Fi input. "
            "Select that input on the amplifier yourself; MA never switches inputs.",
        ),
        ConfigEntry(
            key="sample_rate",
            type=ConfigEntryType.INTEGER,
            label="PCM sample rate",
            default_value=48000,
            required=True,
            options=[
                ConfigValueOption(value=48000, title="48000 Hz (default)"),
                ConfigValueOption(value=44100, title="44100 Hz (experimental)"),
            ],
        ),
        ConfigEntry(
            key="gain",
            type=ConfigEntryType.STRING,
            label="Software gain (not hardware volume)",
            default_value="0.25",
            required=True,
            options=[
                ConfigValueOption(value=f"{value:g}", title=f"{value:g}")
                for value in (0.05, 0.1, 0.25, 0.5, 0.8, 1.0)
            ],
            validate=_valid_gain,
            description="Attenuation only, greater than 0 and at most 1. "
            "Start quietly and set safe hardware volume manually.",
        ),
        ConfigEntry(
            key="max_volume_db",
            type=ConfigEntryType.INTEGER,
            label="Maximum hardware volume (dB)",
            default_value=-10,
            required=True,
            range=(-60, 0),
            description="MA's 0-100 volume slider maps -96.5 dB to this ceiling, "
            "rounded to 0.5 dB. Applies only to commands from this provider; "
            "the physical remote can exceed it. Saving settings never changes amp volume.",
        ),
        ConfigEntry(
            key="local_address",
            type=ConfigEntryType.STRING,
            label="Local IPv4 address",
            default_value="0.0.0.0",
            required=True,
            description="0.0.0.0 listens on all interfaces; sender chooses its routed address.",
        ),
    )
