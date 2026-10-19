"""UDP status reception and confirmed hardware volume/power commands."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from .control import power_packet, volume_db, volume_packet
from .protocol import AirError, check_crc

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpertStatus:
    address: str
    name: str
    powered: bool
    muted: bool
    input_index: int
    input_labels: tuple[str, ...]
    input_enabled: tuple[bool, ...]
    volume_db: float
    received_at: float


def parse_status(data: bytes, address: str) -> ExpertStatus:
    if len(data) not in (345, 512) or data[:2] != b"Da":
        raise AirError("Unsupported Expert status length or magic")
    check_crc(data)

    def label(start: int, length: int) -> str:
        return data[start : start + length].split(b"\0", 1)[0].decode("ascii").strip()

    try:
        return ExpertStatus(
            address=str(ipaddress.IPv4Address(address)),
            name=label(19, 32),
            powered=bool(data[307] & 0x80),
            muted=bool(data[308] & 2),
            input_index=(data[308] & 0x3C) >> 2,
            input_labels=tuple(label(53 + i * 17, 16) for i in range(15)),
            input_enabled=tuple(data[52 + i * 17] == ord("1") for i in range(15)),
            volume_db=(data[310] - 195) / 2,
            received_at=time.monotonic(),
        )
    except (UnicodeDecodeError, ipaddress.AddressValueError) as error:
        raise AirError(f"Invalid Expert status: {error}") from error


def check_ready(status: ExpertStatus, address: str, network_input_index: int | None = None) -> None:
    if status.address != address or not 0 <= time.monotonic() - status.received_at <= 2:
        raise AirError("Expert status is missing, stale, or for a different endpoint")
    if not status.powered or status.muted:
        raise AirError("Expert must already be powered on and unmuted")
    if network_input_index is not None and not 0 <= network_input_index < 15:
        raise AirError("Network input index must be 0..14")
    index = status.input_index
    if not 0 <= index < 15 or not status.input_enabled[index]:
        raise AirError("Selected Expert input is not enabled")
    if network_input_index is not None:
        if index != network_input_index:
            raise AirError("Select the configured network input on the Expert first")
    else:
        network = [
            i
            for i, label in enumerate(status.input_labels)
            if status.input_enabled[i]
            and label.casefold() in ("ethernet", "wifi", "wi-fi", "wi fi", "air")
        ]
        if network != [index]:
            raise AirError("Network input is ambiguous; configure its index explicitly")


class Discovery:
    def __init__(
        self,
        callback: Callable[[ExpertStatus], None] | None = None,
        local_address: str = "0.0.0.0",
        *,
        port: int = 45454,
    ) -> None:
        self.callback = callback
        self.local_address = str(ipaddress.IPv4Address(local_address))
        self.port = port
        self.devices: dict[str, ExpertStatus] = {}
        self._socket: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._control_sequence = 0

    def _control_socket(self) -> socket.socket:
        if self._task is None or self._socket is None:
            raise AirError("Discovery has not been started")
        if self._task.done():
            self._task.result()
            raise AirError("Discovery receiver stopped")
        return self._socket

    def _control_status(self, address: str, *, require_powered: bool = True) -> ExpertStatus:
        self._control_socket()
        status = self.devices.get(address)
        if status is None or not 0 <= time.monotonic() - status.received_at <= 2:
            raise AirError("No recent Expert status for hardware control")
        if require_powered and not status.powered:
            raise AirError("Expert must already be powered on for volume control")
        return status

    async def _send_control(self, address: str, build: Callable[[int], bytes]) -> None:
        sock = self._control_socket()
        loop = asyncio.get_running_loop()
        # Same four sequence-numbered datagrams as HA, on the existing socket.
        for _ in range(4):
            packet = build(self._control_sequence)
            self._control_sequence = (self._control_sequence + 1) % 512
            await loop.sock_sendto(sock, packet, (address, 45455))

    async def set_power(
        self, address: str, powered: bool, *, confirmation_seconds: float | None = None
    ) -> None:
        """Wake/standby only from fresh status; tolerate a boot broadcast gap."""
        if type(powered) is not bool:
            raise ValueError("Power requires a boolean.")
        if confirmation_seconds is None:
            confirmation_seconds = 60 if powered else 5
        async with self._control_lock:
            status = self._control_status(address, require_powered=False)
            if status.powered == powered:
                return
            before = status.received_at
            try:
                async with asyncio.timeout(confirmation_seconds):
                    await self._send_control(address, lambda seq: power_packet(powered, seq))
                    while True:
                        self._control_socket()
                        current = self.devices.get(address)
                        if (
                            current is not None
                            and current.received_at > before
                            and 0 <= time.monotonic() - current.received_at <= 2
                            and current.powered == powered
                        ):
                            return
                        await asyncio.sleep(0.05)
            except TimeoutError as error:
                raise AirError("No confirming Expert power status within timeout") from error
            except OSError as error:
                raise AirError(f"Cannot send Expert power command: {error}") from error

    async def set_volume(
        self, address: str, level: int, maximum_db: int, *, confirmation_seconds: float = 5
    ) -> None:
        """Send only on explicit user command; require a newer matching broadcast."""
        target = volume_db(level, maximum_db)
        async with self._control_lock:
            status = self._control_status(address)
            if status.volume_db == target:
                return
            before = status.received_at
            try:
                async with asyncio.timeout(confirmation_seconds):
                    await self._send_control(address, lambda seq: volume_packet(target, seq))
                    while True:
                        status = self._control_status(address)
                        if status.received_at > before and status.volume_db == target:
                            return
                        await asyncio.sleep(0.05)
            except TimeoutError as error:
                raise AirError("No confirming Expert volume status within timeout") from error
            except OSError as error:
                raise AirError(f"Cannot send Expert volume command: {error}") from error

    async def start(self) -> None:
        if self._task:
            raise AirError("Discovery is already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.local_address, self.port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._socket = sock
        self._task = asyncio.create_task(self._receive(sock), name="air-discovery")

    async def _receive(self, sock: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, endpoint = await loop.sock_recvfrom(sock, 2048)
            try:
                status = parse_status(data, endpoint[0])
            except AirError as error:
                LOGGER.warning("Rejected status from %s: %s", endpoint, error)
                continue
            if endpoint[1] != self.port:
                LOGGER.warning("Rejected Expert status from unexpected source port %s", endpoint)
                continue
            # Bound inventories on untrusted broadcast networks.
            if status.address not in self.devices and len(self.devices) >= 64:
                oldest = min(self.devices, key=lambda addr: self.devices[addr].received_at)
                del self.devices[oldest]
            self.devices[status.address] = status
            self._changed.set()
            if self.callback:
                self.callback(status)

    def require_ready(
        self, expert_address: str, network_input_index: int | None = None
    ) -> ExpertStatus:
        if self._task is None:
            raise AirError("Discovery has not been started")
        if self._task.done():
            self._task.result()
            raise AirError("Discovery receiver stopped")
        status = self.devices.get(expert_address)
        if status is None:
            raise AirError("No Expert status received; check broadcast routing and network input")
        check_ready(status, expert_address, network_input_index)
        return status

    async def wait_for_ready(
        self,
        expert_address: str,
        network_input_index: int | None = None,
        timeout: float = 4,  # noqa: ASYNC109 - bounded convenience API for provider readiness
    ) -> ExpertStatus:
        async with asyncio.timeout(timeout):
            while True:
                self._changed.clear()
                if self._task is None:
                    raise AirError("Discovery has not been started")
                if self._task.done():
                    self._task.result()
                status = self.devices.get(expert_address)
                if status is not None and 0 <= time.monotonic() - status.received_at <= 2:
                    check_ready(status, expert_address, network_input_index)
                    return status
                try:
                    await asyncio.wait_for(self._changed.wait(), 0.1)
                except TimeoutError:
                    # Poll task health even when no status datagrams arrive.
                    if self._task.done():
                        self._task.result()

    async def close(self) -> None:
        task, self._task = self._task, None
        try:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            if self._socket:
                self._socket.close()
                self._socket = None
