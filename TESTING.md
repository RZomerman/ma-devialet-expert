# Testing and reporting

## Offline checks

Python 3.12+ is sufficient for core/simulated tests:

```console
python -m venv .venv
# Activate .venv for your shell.
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -t . -v
python -m ruff check src tests music_assistant_provider tools
python -m mypy
```

These tests use generated PCM and loopback UDP. They do not send audio or
hardware-control commands to an amplifier. Some tests bind loopback ports;
run test suites sequentially, not as concurrent processes.

Release preparation in a clean Windows Python 3.12 environment ran **97 tests:
91 passed, six optional skips**, with Ruff and strict core mypy passing.
The expanded Python 3.14 environment ran 97 tests with three optional skips.
Skip counts depend on installed MA models, the optional sibling checkout and
external C# reference fixtures; they are reported, not treated as executed.
The packaged source also built successfully in the pinned Linux amd64 MA
2.10.6 / Python 3.14.7 image. Its isolated runtime suite ran **97 tests:
94 passed, three optional skips**, including the real provider import and
15-second simulated-loss playback at each sample rate:

| Rate | Source frames | Unique blocks | Resends |
| --- | ---: | ---: | ---: |
| 48000 Hz | 720109 | 3601 | 14 |
| 44100 Hz | 661609 | 3309 | 12 |

This validation used a separate manual-start build-check app, not the active
MA server, and sent no physical playback or hardware-control commands.

An example GitHub Actions matrix and pinned-image test job is included at
[ci/github-actions.yml](ci/github-actions.yml). It is **not active**: the
publishing login did not have GitHub's workflow scope. A repository owner can
review it and copy it to `.github/workflows/offline.yml` through GitHub's web
editor or an appropriately authorized Git client.

Optional checks:

- Exact MA model/source contract checks need the supported MA dependencies and
  pinned source checkout (`DEVIALET_MA_SOURCE`). They skip explicitly otherwise.
- Real MA import checks need a supported Linux MA runtime and
  `DEVIALET_MA_IMPORT=1`.
- `DEVIALET_AIR_SOAK_SECONDS=15` enables sustained loopback provider cases at
  both rates. This is simulated loss/retransmission, not a physical burn-in.
- The external C# cross-check requires Windows PowerShell and the separate
  research checkout. The standalone distribution skips it when absent.

To stage an isolated HA build-check app, use the installation guide's staging
command with a different new destination and **without** `--interactive-server`.
It runs offline checks and exits; it does not start MA or transmit to an amp.
The separate `--playback-trial` mode is a developer tool: physical audio
requires explicit configuration/confirmation. Do not enable it casually.

## Evidence and limits

Prior development validated native Linux amd64 MA 2.10.6 playback to one regular
Expert. A five-minute radio listening test was confirmed clean by the operator.
Hardware-volume commands were confirmed in amplifier status while idle and
during a short radio test.

Longer live radio sessions stopped with reception-progress-stalled errors after
approximately 26 and 39 minutes. They must not be advertised as reliability
passes. The watchdog fails explicitly rather than silently dropping/restarting.
Whether standby caused all such failures has not been established.

Wake encoding matched the installed HA integration for 1024 state/sequence
combinations. Offline wake tests exercise broadcast gaps, fresh confirmation,
timeout/cancellation and readiness-before-audio. **A full physical standby-to-on
cycle is still unverified.**

44.1 kHz physical playback, ARM64 runtime, calibrated level/bit-perfect output,
unattended recovery and multiple amplifier support are not established.

## Suggested physical test

Only test with the operator present and permission to change device state:

1. Set the amp to -40 dB or lower; keep software gain conservative.
2. Verify available state, actual dB, power, mute and the selected network input.
3. Play briefly, stop, confirm idle and absence of `air_error`.
4. Test a small **downward** MA volume command. Confirm actual dB, not just the
   frontend slider; test physical-remote state reflection separately.
5. For wake, put an idle amp in network standby, then request Play. Verify ON
   confirmation precedes audio, with volume/input unchanged.
6. Stop and verify no test timers or audio remain. Keep a rollback path.

## Bug reports

Use GitHub Issues. Include:

- Release tag/commit, MA version, host OS/architecture, Expert model/firmware.
- Wi-Fi/Ethernet, network layout and whether another sender/listener ran.
- Time to failure; powered/muted/input state before and after.
- Redacted player diagnostics: frames, B/F, resend count and exact error.
- Whether playback stopped naturally, was canceled, timed out or hit a watchdog.

Do not include tokens, passwords, backups, auth/library databases, private audio
or identifiable raw captures. B/F counters are transport observations, not proof
of DAC position or acoustic fidelity.
