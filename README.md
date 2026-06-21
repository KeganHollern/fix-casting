# fix-casting

Cast a **full browser tab** to a Chromecast or Google TV on your local network — video and audio from that tab only. This mirrors what you see in the browser window; it does not use Chrome's built-in "cast this video" / dominant-media detection.

```bash
cast "https://example.com/watch"
```

## What it does

1. Opens the URL in a dedicated Chrome window
2. Captures tab frames at a steady frame rate
3. Taps audio from that Chrome instance only (other Mac apps keep their normal output)
4. Encodes video + audio to HLS with ffmpeg
5. Tells your Chromecast to play the stream

Buffered mode is on by default (~45s delay on the TV) for smoother, higher-quality playback.

## Requirements

| Requirement | Notes |
|---|---|
| **macOS 14.2+** | Required for per-tab audio capture via [AudioTee](https://github.com/makeusabrew/audiotee) |
| **Python 3.10+** | |
| **Google Chrome** | Used via Playwright (`channel="chrome"`) |
| **ffmpeg** | With H.264 encoding (`h264_videotoolbox` on Apple Silicon recommended) |
| **Chromecast / Google TV** | On the same LAN as your Mac |
| **Swift** (optional) | Only needed to build AudioTee if not pre-built |

## Install

```bash
git clone <this-repo>
cd fix-casting
./install.sh
```

This creates a virtualenv, installs Python dependencies, downloads Playwright's Chromium (fallback), builds AudioTee when Swift is available, and installs a `cast` command to `~/.local/bin/cast`.

Make sure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Install ffmpeg if needed:

```bash
brew install ffmpeg
```

## Usage

```bash
cast "https://streamfree.app/embed/soccer/ecuador-vs-ivory-coast?quality=1080p&category=soccer"
```

The CLI discovers Chromecast devices on your network and prompts you to pick one. A Chrome window opens locally showing the page; the TV plays the mirrored stream.

Press `Ctrl+C` to stop.

### Options

```
cast <url> [options]

  --width WIDTH           Viewport width (default: 1920)
  --height HEIGHT         Viewport height (default: 1080)
  --fps FPS               Encode frame rate (default: 30 buffered, 23–24 unbuffered)
  --jpeg-quality Q        Tab-capture JPEG quality 1–100 (default: 75 at 1080p+, 80 otherwise)
  --buffered / --no-buffered
                          Buffered mode for quality vs latency (default: buffered)
  --no-audio              Video only, skip tab audio capture
  --audio-offset-ms MS    Manual A/V trim; positive delays audio (default: 0)
  --headless              Hide the local browser window (may break some players)
  --discovery-timeout SEC Seconds to search for devices (default: 5)
  --stats                 Print pipeline timing stats every 10s (diagnose lag)
  --stats-interval SEC    Seconds between stats reports (default: 10)
  --tv-poll-interval SEC  Seconds between Chromecast status polls when --stats is set (default: 2)
```

Video is always encoded as H.264 (universally supported on Chromecast) and
captured via CDP `Page.startScreencast`.

### Examples

Lower latency (less buffering on the TV):

```bash
cast --no-buffered "https://example.com"
```

720p for less CPU usage:

```bash
cast --width 1280 --height 720 "https://example.com"
```

Lower capture quality to cut CPU/bandwidth (or raise it for a sharper image):

```bash
cast --jpeg-quality 60 "https://example.com"
```

Video only (no audio tap):

```bash
cast --no-audio "https://example.com"
```

Smoother 60fps (needs a Chromecast that supports 1080p60):

```bash
cast --fps 60 "https://example.com"
```

Dial in lip-sync if audio leads video (positive delays audio):

```bash
cast --audio-offset-ms 200 "https://example.com"
```

## How it works

```
URL → Chrome tab → JPEG frames + PCM audio
                        ↓
                   ffmpeg (HLS)
                        ↓
              HTTP server on your LAN
                        ↓
              Chromecast plays stream.m3u8
```

- **Video capture** uses CDP `Page.startScreencast`: Chrome pushes JPEG frames as the page paints (up to ~60fps), and every frame is acknowledged with `Page.screencastFrameAck` so the stream never stalls.
- **Even-paced encoding** samples the latest frame at a constant cadence on one thread and feeds ffmpeg on another, with a bounded queue between them. Even sampling keeps motion smooth (no judder) even when an ffmpeg write stalls on an HLS segment flush, while the constant rate keeps the TV buffer from draining. ffmpeg is restarted automatically if it dies or stays backpressured.
- **Audio capture** uses a vendored [AudioTee](https://github.com/makeusabrew/audiotee) binary to tap only the cast browser's processes. Your other apps are not routed through a virtual audio device.
- **Streaming** uses ffmpeg to mux H.264 + AAC into an HLS playlist served from `/tmp/cast-tab-stream/`.
- **Casting** uses [pychromecast](https://github.com/home-assistant-libs/pychromecast) to load the HLS URL on the default media receiver.

## Troubleshooting

**No Chromecast found**  
Ensure the TV and Mac are on the same network. Try increasing `--discovery-timeout`.

**No audio on TV**  
Audio requires AudioTee. Re-run `./install.sh` or build manually:

```bash
cd vendor/audiotee && swift build -c release
```

If audio still fails, start playback in the local Chrome window (click Play). The tool retries autoplay automatically.

**Frozen or choppy video**  
Try `--no-buffered` to rule out buffer-related delay, or lower resolution with `--width 1280 --height 720`.

**High CPU**  
Lower `--fps`, resolution, `--jpeg-quality`, or use `--no-buffered`.

**Lag builds up over time**  
Run with `--stats` and watch which stage drifts:

```bash
cast --stats "https://example.com"
```

Every 10 seconds you'll see something like:

```
[stats] capture 28.5/30 fps, capture avg 35ms peak 52ms, behind 3x
[stats] encode  30.0/30 fps to ffmpeg, frame age avg 8ms peak 20ms, stdin write avg 0.5ms
[stats] hls     12 segments, newest segment 1.2s old
[stats] tv      PLAYING, playback position 142s
```

How to read it:

- **capture fps drops** or **capture ms rises** → Chrome tab capture is the bottleneck (CPU or page complexity)
- **behind Nx** → capture is missing its schedule and skipping ticks
- **encode fps drops** but capture is fine → ffmpeg encoding is struggling
- **frame age rises** → encoder is feeding ffmpeg stale frames (usually means capture slowed down)
- **newest segment age rises** → ffmpeg/HLS segment generation is falling behind
- **tv position** creeping further behind real time → TV buffer or network (expected ~45s with `--buffered`)

## Project layout

```
cast_tab/
  cli.py       Command-line entry point
  browser.py   Chrome tab capture
  streamer.py  ffmpeg HLS encoder + HTTP server
  caster.py    Chromecast playback
  audio.py     Per-tab audio via AudioTee
  devices.py   mDNS Chromecast discovery
vendor/audiotee/   Vendored AudioTee (with stereo mixdown patch)
install.sh         Setup script
```

## License

See individual dependencies: pychromecast, Playwright, AudioTee.