# Devialet Expert AIR for Music Assistant

Experimental native network streaming from **Music Assistant 2.10.6** to a
regular **Devialet Expert (non-Pro, without Core Infinity)**. No Windows AIR
driver, Windows bridge or vendor binaries are required.

**Alpha software: test at low amplifier volume.** Long radio sessions have
stopped with `AIR reception progress stalled`. Recovery requires a new play
request; unattended playback and production reliability are not guaranteed.

## What is included

- Portable Python AIR transport: stereo PCM16 at 48 kHz; 44.1 kHz experimental.
- MA player provider: play/stop, native hardware-volume slider and power controls.
- Wake on a requested play, with up to 60 seconds for confirmed network-standby
  startup. Input and mute are checked afterward, not changed automatically.
- Bounded buffering, feedback/retransmission, cancellation and explicit errors.
- Pinned Docker build and a Home Assistant **local app** staging helper.
- Offline regression tests, source installer and a portable Python wheel.

**This is not an official MA plugin-store entry or a HACS integration.**
The wheel alone does not install the MA provider. Use the custom image/local
app below; an unmodified official MA app cannot load this overlay dynamically.

## Install

Start with the [installation guide](INSTALL.md):

1. **Home Assistant OS / Supervised:** generate and install a separate local app.
2. **Linux Docker:** build the pinned MA image and use a separate persistent
   data directory.
3. **Developers:** install the overlay into an explicitly pinned MA source tree.

Download the tagged [alpha release](https://github.com/RZomerman/ma-devialet-expert/releases)
for the complete source bundle, wheel and SHA-256 checksums.
The Docker image is built locally; there is no published container image.

## Safety and controls

- Start with hardware volume at **-40 dB or lower**.
- Software gain defaults to **0.25**. Values through **1.0** are supported;
  one local listening setup preferred 1.0, but that is not a universal safe
  level or calibrated full scale. Change gain while playback is stopped.
- MA's 0..100 volume slider changes the amplifier, mapping -96.5 dB to a
  configurable ceiling (default **-10 dB**). The physical remote can exceed
  that ceiling; it is not an amplifier-wide limiter.
- Wake requires recent network status. Deep sleep with no network broadcasts,
  mains-off, mute and wrong input are not automatically repaired.
- A separate HA Devialet integration conflicts with AIR's exclusive UDP 45454
  listener. Disable that entry while this provider is active, not HA itself.
- No native mute, input selection, pause, seek, grouping, synchronized playback,
  gapless or high-rate/native PCM24 support is claimed.

## Validation and known limits

See [testing and reporting](TESTING.md). Native Linux amd64 playback has been
heard and confirmed by an operator; hardware-volume commands passed real
idle/playing tests. ARM64 is an intended build target, **not hardware-tested**.
Power encoding and wake lifecycle passed offline tests, but a complete physical
standby-to-on cycle has **not yet been verified**.

The current complete standalone suite has optional skips for dependencies and
external C# reference evidence. Simulated UDP tests are not acoustic validation.
Short successful playback does not resolve the known long-run stall.

## License and provenance

GPL-3.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Control wire format is based on
[ha-devialet-expert](https://github.com/rzomerman/ha-devialet-expert).
AIR research is maintained separately in
[devialet-AIR-protocol](https://github.com/RZomermanMS/devialet-AIR-protocol).
This project is not affiliated with Devialet or the Music Assistant project.
