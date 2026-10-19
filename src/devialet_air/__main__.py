"""Opt-in local WAV playback and passive discovery; no vendor software required."""

from __future__ import annotations

import argparse
import asyncio
import logging
import wave
from collections.abc import AsyncIterator
from dataclasses import asdict

from . import AirClient, AirError, Discovery


async def _main(args: argparse.Namespace) -> None:
    discovery = Discovery(local_address=args.local_address or "0.0.0.0")
    await discovery.start()
    try:
        if args.command == "discover":
            await asyncio.sleep(args.seconds)
            for status in discovery.devices.values():
                print(asdict(status))
            if not discovery.devices:
                raise AirError("No Expert broadcast received on UDP 45454")
            return
        status = await discovery.wait_for_ready(args.expert, args.input_index)
        with wave.open(args.wav, "rb") as wav:
            if (
                wav.getnchannels() != 2
                or wav.getsampwidth() != 2
                or wav.getframerate() not in (44100, 48000)
                or wav.getcomptype() != "NONE"
            ):
                raise AirError("Standalone playback requires a PCM16 stereo WAV at 44100/48000 Hz")

            async def source() -> AsyncIterator[bytes]:
                while chunk := await asyncio.to_thread(wav.readframes, 4096):
                    yield chunk

            client = AirClient(args.expert, args.local_address, wav.getframerate(), args.gain)
            stats = await client.play(
                source(),
                status=status,
                network_input_index=args.input_index,
                status_provider=lambda: discovery.require_ready(args.expert, args.input_index),
            )
            print(asdict(stats))
    finally:
        await discovery.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="Listen passively for Expert status broadcasts")
    discover.add_argument("--seconds", type=float, default=4)
    discover.add_argument("--local-address")
    play = sub.add_parser("play", help="Send an existing PCM16 WAV to an Expert")
    play.add_argument("wav")
    play.add_argument("--expert", required=True)
    play.add_argument("--local-address")
    play.add_argument("--input-index", type=int)
    play.add_argument("--gain", type=float, default=0.25)
    play.add_argument("--confirm-play", action="store_true")
    args = parser.parse_args()
    if args.command == "play" and not args.confirm_play:
        parser.error("Physical playback requires --confirm-play; first set a low amplifier volume")
    if args.command == "discover" and not 0 < args.seconds <= 60:
        parser.error("--seconds must be in (0, 60]")
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_main(args))
    except (AirError, OSError, TimeoutError, ValueError, wave.Error) as error:
        parser.exit(1, f"AIR failed: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Playback/discovery cancelled; buffered receiver audio may finish.\n")


if __name__ == "__main__":
    main()
