"""One-shot real MA trial. Default is decode-only; physical playback is opt-in."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import struct
import wave
from importlib.metadata import version
from pathlib import Path

RATE = 48000
FRAMES = RATE * 5


def make_tone(path: Path) -> None:
    """Generate five seconds of quiet stereo tone with 100 ms fades."""
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, RATE, FRAMES, "NONE", "not compressed"))
        for start in range(0, FRAMES, 200):
            chunk = bytearray()
            for frame in range(start, start + 200):
                fade = min(1.0, frame / 4800, (FRAMES - 1 - frame) / 4800)
                left = round(8192 * fade * math.sin(2 * math.pi * 440 * frame / RATE))
                right = round(8192 * fade * math.sin(2 * math.pi * 660 * frame / RATE))
                chunk.extend(struct.pack("<hh", left, right))
            output.writeframesraw(chunk)


def trial_settings(stream_bind: str) -> dict[str, object]:
    return {
        "core": {
            "webserver": {
                "domain": "webserver",
                "values": {"bind_ip": "127.0.0.1", "bind_port": 18095},
            },
            "streams": {
                "domain": "streams",
                "values": {
                    "bind_ip": stream_bind,
                    "bind_port": 18097,
                    "publish_ip": stream_bind,
                },
            },
        }
    }


def prepare_files(attempt: str, confirmed: bool) -> tuple[Path, Path, Path]:
    root = Path("/data") / "ma-trials" / attempt
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "playback_attempted"
    if confirmed and marker.exists():
        raise RuntimeError("This attempt already requested audio; use a new explicitly approved ID")
    storage = root / "storage"
    storage.mkdir(exist_ok=True)
    settings = storage / "settings.json"
    if not settings.exists():
        settings.write_text(json.dumps(trial_settings("127.0.0.1")), encoding="utf-8")
    tone = root / "quiet-test.wav"
    make_tone(tone)
    return storage, tone, marker


async def run() -> None:
    if version("music_assistant") != "2.10.6":
        raise RuntimeError("This trial requires MA 2.10.6")
    options = json.loads(
        await asyncio.to_thread(Path("/data/options.json").read_text, encoding="utf-8")
    )
    attempt = options["attempt_id"]
    if not isinstance(attempt, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", attempt):
        raise ValueError("Invalid attempt_id")
    confirmed = options["confirm_playback"]
    if not isinstance(confirmed, bool):
        raise ValueError("confirm_playback must be a boolean")
    expert = str(ipaddress.IPv4Address(options["expert"])) if confirmed else None
    index = options["network_input_index"]
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 14:
        raise ValueError("Invalid network_input_index")
    storage, tone, marker = await asyncio.to_thread(prepare_files, attempt, confirmed)
    root = storage.parent
    # This is a separate standalone MA process, not an ingress-enabled replacement add-on.
    os.environ.pop("SUPERVISOR_TOKEN", None)
    os.environ.pop("HASSIO_TOKEN", None)
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.devialet_air.provider import DevialetAirProvider
    from music_assistant_models.enums import ContentType
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.setup_flow import FlowStepType

    mass = MusicAssistant(str(storage), str(root / "cache"), safe_mode=True)
    provider: DevialetAirProvider | None = None
    try:
        async with asyncio.timeout(90):
            await mass.start()
        if mass.webserver.publish_port != 18095 or mass.streams.publish_port != 18097:
            raise RuntimeError("MA did not honor the isolated server ports")
        if not ipaddress.ip_address(mass.streams.bind_ip).is_loopback:
            raise RuntimeError("MA stream listener did not remain on loopback")
        media = PlayerMedia(uri=str(tone), title="Quiet AIR test: 440/660 Hz", duration=5)
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE, sample_rate=RATE, bit_depth=16, channels=2
        )
        count = 0
        async with asyncio.timeout(15):
            async for chunk in mass.streams.get_stream(media, pcm_format, force_flow_mode=True):
                count += len(chunk)
        if count != FRAMES * 4:
            raise RuntimeError(f"Real MA decoder returned {count} bytes, expected {FRAMES * 4}")
        print(
            f"AIR_MA_DECODE_OK frames={FRAMES} rate={RATE} physical_audio={confirmed}", flush=True
        )
        if not confirmed:
            return
        assert expert is not None
        step = await mass.config.setup_provider("devialet_air")
        step = await mass.config.submit_setup_flow(
            step.flow_id,
            {
                "host": expert,
                "network_input_index": index,
                "sample_rate": RATE,
                "gain": 0.25,
                "local_address": "0.0.0.0",
            },
        )
        if step.type != FlowStepType.FINISH or not step.result:
            raise RuntimeError(f"AIR setup did not finish: {step.to_dict()}")
        instance_id = step.result["instance_id"]
        async with asyncio.timeout(15):
            await mass.get_provider_ready_event(instance_id).wait()
        loaded = mass.get_provider(instance_id)
        if not isinstance(loaded, DevialetAirProvider) or loaded.player is None:
            raise RuntimeError("The real AIR provider did not register its player")
        provider = loaded
        player = provider.player
        status = await provider.discovery.wait_for_ready(expert, index)
        if status.volume_db > -40:
            raise RuntimeError(f"Hardware volume is not low enough: {status.volume_db} dB")
        print(
            f"AIR_MA_READY player={player.player_id} input={status.input_index} "
            f"hardware_volume_db={status.volume_db}",
            flush=True,
        )
        await asyncio.to_thread(
            marker.write_text, "Physical playback explicitly requested once.\n", encoding="utf-8"
        )
        async with asyncio.timeout(20):
            await player.play_media(media)
            print("AIR_MA_STARTED", flush=True)
            if player._play_task is None:
                raise RuntimeError("AIR session ended before its completion could be observed")
            await asyncio.shield(player._play_task)
        stats = dict(player._extra_attributes)
        if "air_error" in stats:
            raise RuntimeError(stats["air_error"])
        if (
            stats.get("air_source_frames") != FRAMES
            or stats.get("air_sent_blocks") != FRAMES // 200
        ):
            raise RuntimeError(f"Incomplete MA audio transmission: {stats}")
        if stats.get("air_acknowledged_frame") != FRAMES - 200:
            raise RuntimeError(f"Missing final AIR acknowledgement: {stats}")
        if stats.get("air_receiver_frame", 0) < FRAMES:
            raise RuntimeError(f"Missing final AIR drain: {stats}")
        print("AIR_MA_COMPLETE " + json.dumps(stats), flush=True)
    finally:
        try:
            if provider is not None and provider.player is not None:
                await provider.player.stop()
        finally:
            await mass.stop()
            print("AIR_MA_STOPPED", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
