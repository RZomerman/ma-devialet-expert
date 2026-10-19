"""Expert control wire format, matching the installed HA devialet_expert integration."""

from __future__ import annotations

import math
import struct

from .protocol import crc16

MIN_VOLUME_DB = -96.5


def _packet(sequence: int) -> bytearray:
    if type(sequence) is not int or not 0 <= sequence < 512:
        raise ValueError("Control sequence must be between 0 and 511.")
    packet = bytearray(142)
    packet[:2] = b"Dr"
    packet[3] = sequence & 0xFF
    packet[5] = sequence >> 1
    return packet


def _finish(packet: bytearray) -> bytes:
    struct.pack_into(">H", packet, 12, crc16(bytes(packet[:12])))
    return bytes(packet)


def power_packet(powered: bool, sequence: int) -> bytes:
    """Encode absolute power (opcode 1), never a toggle or Wake-on-LAN guess."""
    if type(powered) is not bool:
        raise ValueError("Power requires a boolean.")
    packet = _packet(sequence)
    packet[6] = int(powered)
    packet[7] = 1
    return _finish(packet)


def volume_db(level: int, maximum_db: int) -> float:
    """Map MA's integer percentage to a bounded half-dB hardware target."""
    if type(level) is not int or not 0 <= level <= 100:
        raise ValueError("Volume level must be an integer between 0 and 100.")
    if type(maximum_db) is not int or not -60 <= maximum_db <= 0:
        raise ValueError("Maximum hardware volume must be an integer from -60 to 0 dB.")
    return round((MIN_VOLUME_DB + level * (maximum_db - MIN_VOLUME_DB) / 100) * 2) / 2


def volume_level(db: float, maximum_db: int) -> int:
    """Reflect physical-remote changes using the same slider scale."""
    if not math.isfinite(db):
        raise ValueError("Hardware volume must be finite.")
    volume_db(0, maximum_db)
    return round(min(100, max(0, 100 * (db - MIN_VOLUME_DB) / (maximum_db - MIN_VOLUME_DB))))


def volume_packet(db: float, sequence: int) -> bytes:
    """Encode Dr opcode 4, including the control header's CRC (not a whole-frame CRC)."""
    if not math.isfinite(db) or not MIN_VOLUME_DB <= db <= 0 or db * 2 != round(db * 2):
        raise ValueError("Volume target must be a half-dB value from -96.5 to 0 dB.")
    packet = _packet(sequence)
    magnitude = abs(db)
    word = 0
    if magnitude:
        word = 0x3F00
        while magnitude > 0.5:
            word += 256 >> math.ceil(1 + math.log2(magnitude))
            magnitude -= 0.5
        if db < 0:
            word |= 0x8000
    packet[7] = 4
    struct.pack_into(">H", packet, 8, word)
    return _finish(packet)
