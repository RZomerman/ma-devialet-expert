# Installation and rollback

## Before starting

- A regular Devialet Expert with AIR Wi-Fi/Ethernet enabled. Other generations
  and firmware versions are not proven compatible.
- Select the intended network input on the amplifier and set hardware volume
  to **-40 dB or lower**. MA will not switch inputs or unmute it.
- Your MA host and amplifier must exchange IPv4 broadcasts on the same LAN.
- Stop your existing MA instance before starting the custom server. Back it up
  first and keep it installed for rollback. Never share one writable data
  directory between two running servers.
- Stop other AIR senders. If using the HA Devialet Expert integration, disable
  its configuration entry while this provider owns status UDP 45454.
- This release is pinned to **MA 2.10.6**. Do not import data from a newer MA
  into this older server or override the image version without porting/testing
  the provider APIs. Standard MA upgrades replace the overlay.

Download and extract the source ZIP from the alpha release, or:

```console
git clone --branch v0.1.0-alpha.1 https://github.com/RZomerman/ma-devialet-expert.git
cd ma-devialet-expert
```

## A. Home Assistant OS / Supervised local app

This is a **local app build**, not a repository URL you can paste into the
Home Assistant app store. Staging needs Python 3.12+ on a computer with write
access to HA's `addons` share (for example through the Samba Share app).
The helper needs no Python dependencies and does not start a server.

On Windows PowerShell, from the extracted repository:

```powershell
py -3.12 .\tools\stage_home_assistant_check.py '\\YOUR_HA_HOST\addons\devialet_air_server' --interactive-server
```

On Linux with the addons share mounted locally:

```console
python3 tools/stage_home_assistant_check.py /mnt/ha-addons/devialet_air_server --interactive-server
```

The destination must **not exist**; the helper refuses to overwrite it.
Do not delete an existing installation to get past that check.

1. In HA, open **Settings > Apps** (called **Add-ons** on some versions).
2. Refresh/check for updates in the store. Find
   **Music Assistant 2.10.6 - Devialet AIR** under **Local apps**.
3. Install it. Supervisor builds the pinned image locally; allow disk space
   and network access for the MA image and build dependencies.
4. Keep the original MA stopped, then start the new app manually.
5. Open its Web UI, or `http://YOUR_HA_HOST:8095`, and create/login to your MA
   account. The app also provides Supervisor ingress.
6. Configure the provider as described below. Enable automatic app startup
   only after testing. Leave automatic watchdog restarts off during diagnosis.

### Optional existing MA profile

Fresh setup is the safest path. For a compatible **2.10.6** profile, the helper
can import a **local, unencrypted, MA-only Supervisor backup** of the official
`d5369777_music_assistant` app.

1. Make that partial backup through Supervisor and keep a separate safe copy.
2. Before the first start, set the new app's `source_backup` option to its
   eight-character backup slug, not a filesystem path.
3. The backup must be present in HA's backup directory. It is mounted read-only.
4. Start the app. A one-time guarded import preserves the new app options,
   refuses an existing profile and records successful completion. A failed
   partial import blocks startup instead of pretending success.

Backups contain authentication/library data: never attach them to an issue or
upload them to GitHub. Media files are not included. Reconfigure missing media
mounts or source credentials through MA; cached library entries alone do not
make those files available.

The local app uses host networking, Supervisor/HA/Auth APIs, manager role,
read-only media/backup mounts, and mount capabilities/AppArmor for CIFS sources.
Review the generated `config.json` and `apparmor.txt` before installing. It does
not require the Docker socket, host PID namespace or disabling protection mode.

## B. Linux Docker

Docker Engine with host networking is required; the recommended host is Linux
amd64. Docker Desktop networking on Windows/macOS has **not** been validated.
ARM64 execution is untested. Do not run the unmodified MA container alongside
this server on the same host/ports.

```console
docker build -f Dockerfile.music-assistant -t ma-devialet-expert:0.1.0-alpha.1 .
docker volume create ma-devialet-test-data
docker run -d --name ma-devialet-expert --network host --restart unless-stopped \
  -v ma-devialet-test-data:/data \
  ma-devialet-expert:0.1.0-alpha.1
docker logs ma-devialet-expert
```

Open `http://YOUR_DOCKER_HOST:8095`. The image retains MA's normal entrypoint;
the HA backup-import wrapper is only used in the generated HA app.
Bind-mount media read-only if needed. Do not give this test container your
production data path without a separate backup and compatible-version checks.
The command intentionally does not include privileges for SMB mounts: use
host-mounted media for this simple Docker setup.

### Network

- Status broadcast: UDP 45454 to the MA host.
- Audio/control destination: Expert UDP 45455.
- Feedback: sender UDP 45456, Expert feedback source UDP 45456.
- Web UI: TCP 8095; other MA services use their normal ports.

The listener binds IPv4 wildcard UDP 45454 exclusively. The configured local
IPv4 option controls the audio sender, not broadcast discovery. The installer
does not change firewalls or network settings.

## Configure MA

In **Settings > Providers > Add provider**, select
**Devialet Expert AIR (experimental)**:

| Setting | Recommended initial value |
| --- | --- |
| Expert IPv4 address | Your amplifier's fixed/reserved IPv4 address |
| Network input index | Its zero-based enabled Wi-Fi/Ethernet input, 0..14 |
| PCM sample rate | 48000 Hz |
| Software gain | 0.25 initially; only raise while idle and hardware volume low |
| Maximum hardware volume | -10 dB initially, or a lower ceiling for your setup |
| Local IPv4 address | 0.0.0.0 unless explicit routing requires otherwise |

Input labels/indices and actual hardware dB are reported in player diagnostics.
Do not assume every Expert uses the same Wi-Fi index.
Select the registered Expert in MA's player picker, then play a short item.
MA must show a fresh, available device; no source audio is sent until readiness
is confirmed.

### Volume, power and wake

- The slider controls **hardware volume**, not software gain. 0% is -96.5 dB,
  not mute; 100% is the configured ceiling. Targets round to half-dB steps.
- Remote changes update MA. Above-ceiling physical settings display 100%;
  actual dB remains visible in diagnostics.
- Hardware commands require fresh status and newer confirmation. A timeout
  does not guarantee the command was not applied: check actual state.
- Requested playback and MA power-on can wake **network standby**. Boot may
  take up to 60 seconds. Deep sleep without status broadcasts is not supported.
- Standby through MA stops this provider's stream first. Ordinary Stop does
  not switch the amplifier off.
- Wake never changes input, mute or volume. Keep those safe manually.
- Do not treat the software ceiling as protection from the physical remote.

## C. Developer source overlay

Use MA's supported Linux/Python environment, not a Windows production server.
The core wheel requires Python 3.12+, but MA 2.10.6 requires its own newer
runtime. The source helper checks the exact MA Git revision and refuses an
existing provider directory.

```console
git clone https://github.com/music-assistant/server.git music-assistant-server
git -C music-assistant-server checkout c24866405bd2068c25a60e1014e5ef8c3f6445bb
# Activate/install that MA checkout's supported development environment first.
python tools/install_provider.py music-assistant-server
```

Use the MA environment's Python, not a separate core-only venv. The helper
installs the local core package editable and copies the provider; it does not
start MA. The wheel by itself contains only the transport, not the MA overlay.

## Upgrade and rollback

Back up the **custom app's current profile** before each update; the old
official profile does not contain changes made since migration.

- Docker: stop/remove only the named custom container, retain its data volume,
  build the new reviewed tag and recreate it with the same volume. Keep the
  previous image for rollback. Do not run two copies on that data.
- HA local app: back up the custom app, stop it, refresh only its known source
  context from a reviewed release, then use Supervisor's Rebuild action.
  Staging into a new temporary directory first avoids the helper's intentional
  no-overwrite guard; compare generated metadata before replacing your context.
  Never replace or delete the app's `/data` profile.
- To return to official MA, stop the custom server, start the retained original
  compatible MA app, then re-enable the HA Devialet integration. Its old profile
  may lack changes made in the custom server. Do not copy newer databases into
  an older MA version.
- Unloading AIR releases UDP 45454/45456. No driver or firewall changes need
  undoing.

## Troubleshooting

- **Unavailable:** check broadcast routing, exclusive UDP listener ownership
  and amplifier network standby. Do not bypass readiness guards.
- **Wrong input/muted:** correct manually; the provider does not switch/unmute.
- **Reception progress stalled:** known open reliability issue. Stop, verify
  device readiness and manually retry. Do not disable watchdogs.
- **File not found / SMB login:** repair the source/mount/credentials in MA.
  Successful radio playback does not prove file-source access.
- **Import error:** inspect the explicit app log, keep original backup, and
  do not remove incomplete-import markers to force startup.

Report redacted diagnostics using [TESTING.md](TESTING.md). Do not upload
credentials, authentication databases, backups or raw private captures.
