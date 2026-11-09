# figpi

A **thin AI agent for the Raspberry Pi 5**, shipped as a ready-to-flash SD card image.

The Pi runs the least possible: one small Python service (about 110 KB, standard library only, no pip)
on stock Raspberry Pi OS Lite. All the thinking happens on an **Ollama server elsewhere in your network**.
The Pi is the agent's body: it exposes a chat page on your LAN, can act on itself with a small set of
tools (GPIO, system status, shell, files, HTTP, long-term notes), and shows a pixel-art bird on an
attached touchscreen that animates while you hold your finger on it.

```
 browser on your phone/laptop ──► http://figpi.local:8080 ──► figpi service on the Pi
                                                               │  tools: gpio, shell, files, ...
                                                               ▼
                                                   Ollama on your PC / NAS / server
```

## Flash and boot

1. Download `figpi-<version>-arm64-lite.img.xz` from the [releases](../../releases) page
   (or build it yourself, see below).
2. Flash it with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) using **Use custom**.
   When Imager offers OS customisation, use it: set a username/password, your Wi-Fi, and enable SSH.
   This is the normal Raspberry Pi OS first-boot setup and works with this image unchanged.
3. Before ejecting the card, open the `bootfs` partition and edit **`figpi.toml`**:

   ```toml
   [ollama]
   url = "http://192.168.1.50:11434"   # or "auto" to scan your /24 for an Ollama server
   model = "qwen3:8b"                   # any tool-capable model installed on that server
   ```

   On the Ollama machine make sure it listens on the network: `OLLAMA_HOST=0.0.0.0 ollama serve`
   (and pull a [tool-capable model](https://ollama.com/search?c=tool)).
4. Boot the Pi. Open `http://figpi.local:8080` (or `http://<pi-ip>:8080`; the IP is printed on the
   HDMI console). The page asks for the web password: the one you put in `figpi.toml`, or the generated
   one in `figpi-password.txt` on the boot partition.

That's it. The conversation persists across reboots; the agent reconnects to Ollama by itself.

### Without Imager (plain `dd`)

Fill in the `[wifi]` section of `figpi.toml`; the first-boot service connects through NetworkManager.
To get SSH, create an empty file called `ssh` on the boot partition and a `userconf.txt`
as described in the Raspberry Pi docs.

## What it can do

| tool group | gives the model                                                         |
|------------|-------------------------------------------------------------------------|
| `system`   | hostname, IPs, CPU temperature, memory, disk, uptime, throttling        |
| `gpio`     | read / drive / configure header pins (BCM numbering, via `pinctrl`)     |
| `shell`    | run bash as root: apt, systemctl, journalctl, i2cdetect, camera, ...    |
| `files`    | read, write, list files under `tools.file_roots`                        |
| `http`     | fetch a URL (HTML reduced to text), e.g. to poke other LAN devices      |
| `memory`   | save / delete long-term notes that are shown to it in every conversation |

Edit `enabled = [...]` under `[tools]` to remove any of these. The service runs as root so that the
model can administer the Pi; the only thing standing between your LAN and that shell is the web
password, so set a real one if the network is not fully yours.

## Touchscreen face

Plug a small HDMI or DSI touchscreen into the Pi and the image shows a **black screen with the bird**
from the moment the service starts; the boot console is silenced too (`quiet`, no cursor, no rainbow
splash, no blanking). **Press and hold the bird and it animates; let go and it stops** (`release = "freeze"`),
or snaps back to its resting frame with `release = "rest"`.

No desktop is involved: `figpi display` paints `/dev/fb0` directly and reads the touch panel from
`/dev/input`, using nothing but the standard library. The bird is a 3 KB sprite baked into the
package; the display service is skipped automatically on a headless Pi (no `/dev/fb0`).

```toml
[display]
scale = 0            # 0 = about a third of the screen height (5x on 1080p); any integer works
release = "freeze"   # or "rest"
touch_rotate = 0     # 90 / 180 / 270 if touches land in the wrong spot
touch_device = ""    # auto-detected; set /dev/input/eventN to pin it
```

To use your own animation, convert any animated WebP/GIF/APNG on your computer and point the config at it:

```bash
pip install pillow
scripts/make-sprite.py my-animation.webp mine.sprite --fps 8   # copy mine.sprite to the boot partition
```

```toml
[display]
sprite = "/boot/firmware/mine.sprite"
```

## Configuration

Everything is in `/boot/firmware/figpi.toml` (the `bootfs` partition). After editing it on the Pi:
`sudo systemctl restart figpi`. Logs: `journalctl -u figpi -f`.

| key                     | default      | meaning |
|-------------------------|--------------|---------|
| `ollama.url`            | `"auto"`     | Ollama base URL, or `auto` to scan the local /24 for port 11434 |
| `ollama.model`          | `""`         | model name; empty picks the first installed model with tool support |
| `ollama.think`          | `false`      | for thinking models: `false` is faster, `true` streams the reasoning |
| `ollama.num_ctx`        | `8192`       | context window requested from Ollama |
| `web.port`              | `8080`       | chat page / API port |
| `web.password`          | `""`         | empty = generated on first boot (`figpi-password.txt`) |
| `tools.enabled`         | all six      | tool groups offered to the model |
| `tools.file_roots`      | `/home`, ... | directories the `files` tools may touch |
| `agent.name`            | `"figpi"`    | assistant name; also the hostname if the Pi still has the stock one |
| `agent.system_prompt`   | `""`         | extra instructions |
| `agent.max_tool_rounds` | `12`         | tool calls per user message before it must answer |
| `wifi.*`                | empty        | SSID / password / country for `dd`-flashed cards |
| `display.*`             | see above    | touchscreen face: sprite, scale, release behaviour, touch mapping |

`OLLAMA_HOST` in the environment overrides `url = "auto"`, so `figpi chat` works on any Linux box too.

## Command line (over SSH)

```bash
figpi status            # connection, model, tools, as JSON
figpi chat              # interactive terminal chat with the same agent and history
figpi ask "cpu temp?"   # one-shot question
```

## HTTP API

Authenticate with `Authorization: Bearer <web.password>`.

| method | path           | purpose |
|--------|----------------|---------|
| POST   | `/api/chat`    | `{"message": "..."}` → streamed JSON lines: `text`, `thinking`, `tool_call`, `tool_result`, `stats`, `error`, `done` |
| GET    | `/api/history` | the persisted conversation |
| POST   | `/api/reset`   | forget the conversation |
| POST   | `/api/connect` | re-scan / reconnect to Ollama |
| GET    | `/api/status`  | model, URL, tools, last error |
| GET    | `/healthz`     | `ok` |

## Building the image yourself

Linux with root (loop devices), plus `curl xz losetup mount python3 mcopy` (`apt install mtools`):

```bash
sudo image/build.sh                 # downloads the latest Raspberry Pi OS Lite arm64, outputs dist/*.img.xz
sudo image/build.sh --no-compress   # faster, leaves a raw .img
```

The script verifies the upstream SHA-256, mounts the root partition by byte offset, copies `figpi/`
to `/opt/figpi`, installs and enables three systemd units (agent, first boot, display), sets the
hostname, drops `figpi.toml` on the boot partition with mtools and adds the quiet-boot options to
`cmdline.txt` / `config.txt`. No chroot, no emulation, no package installs: the agent uses the
OS's own `python3`. CI (`.github/workflows/ci.yml`) runs the tests on every push and builds and
attaches the image to a GitHub release on `v*` tags, or on demand via *Run workflow*.

## Installing on a Pi you already have

```bash
git clone https://github.com/rthauby/figpi && cd figpi
sudo scripts/install.sh
```

## Layout

```
figpi/            the agent (stdlib only)
  agent.py        the loop: user → Ollama → tools → Ollama → answer, persisted history
  ollama.py       streaming /api/chat client, model picking, LAN discovery
  tools.py        the tool set
  server.py       HTTP server + chat page (figpi/web/index.html)
  cli.py          serve / chat / ask / status / firstboot / display
  display.py      framebuffer + touchscreen "face" (stdlib ioctls, no desktop)
  assets/         bird.sprite, the baked animation
  config.py       figpi.toml loader
assets/bird.webp  the source animation (scripts/make-sprite.py turns it into figpi/assets/bird.sprite)
image/
  build.sh        image builder
  quiet-boot.py   kernel/firmware options that keep the screen black during boot
  overlay/        files copied onto the root filesystem (units, launcher, console banner)
  boot/figpi.toml default config placed on the boot partition
scripts/install.sh  install on a running Pi
tests/            pytest suite with a scripted fake Ollama server
```

## Development

```bash
python -m pip install pytest
python -m pytest -q
OLLAMA_HOST=192.168.1.50:11434 python -m figpi chat   # run the agent on your laptop
```

MIT licensed.
