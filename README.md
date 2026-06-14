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
  --codec {auto,h264,hevc,av1}
                          Video codec (default: auto = H.264)
  --buffered / --no-buffered
                          Buffered mode for quality vs latency (default: buffered)
  --no-audio              Video only, skip tab audio capture
  --headless              Hide the local browser window (may break some players)
  --discovery-timeout SEC Seconds to search for devices (default: 5)
```

### Examples

Lower latency (less buffering on the TV):

```bash
cast --no-buffered "https://example.com"
```

720p for less CPU usage:

```bash
cast --width 1280 --height 720 "https://example.com"
```

Video only (no audio tap):

```bash
cast --no-audio "https://example.com"
```

Older Chromecast that rejects HEVC (auto already uses H.264):

```bash
cast --codec h264 "https://example.com"
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

- **Video capture** uses paced tab screenshots. CDP screencast stops updating once hardware-accelerated video plays, which caused frozen frames on the TV.
- **Audio capture** uses a vendored [AudioTee](https://github.com/makeusabrew/audiotee) binary to tap only the cast browser's processes. Your other apps are not routed through a virtual audio device.
- **Streaming** uses ffmpeg to mux H.264 + AAC into an HLS playlist served from `/tmp/cast-tab-stream/`.
- **Casting** uses [pychromecast](https://github.com/home-assistant-libs/pychromecast) to load the HLS URL on the default media receiver.

## Troubleshooting

**No Chromecast found**  
Ensure the TV and Mac are on the same network. Try increasing `--discovery-timeout`.

**TV shows idle / stream rejected**  
Use `--codec h264`. Older Chromecasts do not support HEVC or AV1 HLS.

**No audio on TV**  
Audio requires AudioTee. Re-run `./install.sh` or build manually:

```bash
cd vendor/audiotee && swift build -c release
```

If audio still fails, start playback in the local Chrome window (click Play). The tool retries autoplay automatically.

**Frozen or choppy video**  
Make sure you are on a recent version of this repo (paced screenshot capture). Try `--no-buffered` to rule out buffer-related delay, or lower resolution with `--width 1280 --height 720`.

**High CPU**  
Lower `--fps`, resolution, or use `--no-buffered`.

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