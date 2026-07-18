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

The production profile is on by default: it keeps the 30fps/high-bitrate encode
settings while using 2-second HLS segments and a six-entry rolling playlist
(12 seconds retained). A conventional player holdback is roughly three target
durations (~6 seconds), but the TV chooses its actual live position. The cast
waits for three complete segments before loading the receiver, giving startup
the same standards-conservative runway.

## Requirements

| Requirement | Notes |
|---|---|
| **macOS 14.2+** | Required for per-tab audio capture via [AudioTee](https://github.com/makeusabrew/audiotee) |
| **Python 3.11+** | |
| **[uv](https://docs.astral.sh/uv/)** | Used by `install.sh` to install the `cast` CLI |
| **Google Chrome** | Used via Playwright (`channel="chrome"`) |
| **ffmpeg** | With H.264 encoding (`h264_videotoolbox` on Apple Silicon recommended) |
| **Chromecast / Google TV** | On the same LAN as your Mac |
| **Swift** (optional) | Only needed to build AudioTee when no prebuilt binary is available |

## Install

First install [uv](https://docs.astral.sh/uv/) (if you don't have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone and run the installer:

```bash
git clone <this-repo>
cd fix-casting
./install.sh
```

This uses [`uv tool install`](https://docs.astral.sh/uv/) to install a
**non-editable snapshot** of the current checkout into `~/.local/bin`, with
runtime dependency versions constrained by `uv.lock`. Changing branches or
editing this checkout therefore does not silently change the installed
command. It also downloads Playwright's Chromium (fallback) and installs
AudioTee under `~/.local/share/fix-casting` for per-tab audio — preferring a
SHA-256-verified prebuilt binary from this repo's GitHub releases and building
from `vendor/audiotee` with Swift only when no verified prebuilt is available.
(Maintainers: push an `audiotee-v*` tag to publish a new prebuilt via the
release workflow.)

Make sure `~/.local/bin` is on your `PATH`:

```bash
uv tool update-shell    # or: export PATH="$HOME/.local/bin:$PATH"
```

Use `cast --version` to see the installed branch, revision, source fingerprint,
and AudioTee hash (or to identify an older editable/source install). To update
later, re-run `./install.sh` from the revision you want. To remove the command:
`uv tool uninstall fix-casting`.

Install ffmpeg if needed:

```bash
brew install ffmpeg
```

## Usage

```bash
cast "https://streamfree.app/embed/soccer/ecuador-vs-ivory-coast?quality=1080p&category=soccer"
```

The CLI discovers Chromecast devices on your network and prompts you to pick one (or pass `--device NAME` to skip the prompt). A Chrome window opens locally showing the page; the TV plays the mirrored stream.

If the TV stops playing the stream (someone exits the receiver app, a stream
error), the watchdog re-casts it automatically within ~10s.

Press `Ctrl+C` to stop; a one-line summary (duration, re-casts, skipped/discarded
video timeline ticks, guarded A/V re-anchors, ffmpeg restarts) prints on exit.

### Options

```
cast <url> [options]

  --version               Show installed version and source provenance
  --width WIDTH           Viewport width (default: 1920)
  --height HEIGHT         Viewport height (default: 1080)
  --fps FPS               Encode frame rate (default: 30 production, 23–24 with --no-buffered)
  --jpeg-quality Q        Tab-capture JPEG quality 1–100 (default: 92)
  --video-bitrate MBPS    Override H.264 target bitrate in Mbps, max 1000 (default: by resolution)
  --buffered / --no-buffered
                          Production 2s/12s HLS profile vs compatible 1s/4s
                          low-latency profile (default: buffered/production)
  --no-audio              Video only, skip tab audio capture
  --audio-offset-ms MS    Manual A/V trim 0–3000; positive delays audio (default: 0)
  --audio-drift-ppm PPM   Correct measured audio-clock drift, -100000…100000 (default: 0)
  --adblock / --no-adblock
                          Block ads/trackers in the captured tab (default: on)
  --headless              Hide the local browser window (may break some players)
  --device NAME           Cast to this device by name, skip the picker (case-
                          insensitive; a unique substring works)
  --discovery-timeout SEC Seconds to search for devices (default: 5)
  --stats                 Print pipeline timing stats every 10s (diagnose lag)
  --stats-interval SEC    Seconds between stats reports (default: 10)
  --tv-poll-interval SEC  Seconds between Chromecast status polls when --stats is set (default: 2)
  --tui                   Live full-screen dashboard of all stats + audio-offset knob
```

Video is always encoded as H.264 (universally supported on Chromecast) and
captured via CDP `Page.startScreencast`.

### Examples

Lower latency (shorter segments and rolling playlist, with leaner encode tuning):

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

Live dashboard with a real-time audio-offset knob:

```bash
cast --tui "https://example.com"
```

### Live dashboard (`--tui`)

`--tui` replaces the scrolling `--stats` text with a full-screen
[Textual](https://textual.textualize.io/) dashboard. Every metric shows a
number, a sparkline of its recent history, and a one-line description, and turns
**yellow → red** as it degrades. A status bar at the top gives an at-a-glance
health dot per segment. Metrics are grouped by pipeline segment:

- **① Capture** — CDP screencast + AudioTee (incoming): capture FPS,
  Chrome→app frame lag, decode time, audio pipe backlog, audio warnings.
- **② Encode pipeline** (internal): encode FPS, frame age, queue depth, ffmpeg
  stdin-write time, repeats/re-anchors.
- **③ HLS stream** (outgoing): segment count, newest-segment age, rotation.
- **④ TV / Chromecast** (playback): state, position, advance-vs-wall-clock,
  micro-stalls, non-playing polls.
- **⑤ A/V sync**: CFR timeline guard/re-anchors, lost video ticks, ffmpeg restarts.

The **audio-offset knob** at the bottom adjusts lip-sync live. Use the
`-100 / -10 / +10 / +100` ms buttons or the keyboard:

| Key | Action |
|---|---|
| `[` / `]` | audio offset −10 / +10 ms |
| `{` / `}` | audio offset −100 / +100 ms |
| `r` | reset offset to 0 |
| `space` | pause / resume the TV (a pause longer than playlist retention resumes as a jump to live) |
| `,` / `.` | TV volume −5% / +5% |
| `m` | mute / unmute the TV |
| `q` | stop the cast and exit |

Changes apply after presses settle (one quick ffmpeg re-sync, so expect a brief
glitch). The TV applies an offset change only after it reaches the restarted HLS
generation. That lag depends on the receiver's live holdback, not the full
playlist-retention window, so adjust in small steps and wait for it to appear.

### Ad blocking (`--adblock`)

On by default: the captured tab blocks ad/tracker requests so ads don't appear
in what you cast (and don't waste bitrate). It derives an ad/tracker **domain**
list from **uBlock Origin's network filter lists + Peter Lowe's ad-server list**
and blocks them **natively in Chrome** via CDP `Network.setBlockedURLs` — no
per-request Python work, so it doesn't steal CPU from the encoder (an earlier
per-request interception approach did, causing video stutter). Lists are fetched
once and cached for a day in `~/.cache/fix-casting/adblock`. Use `--no-adblock`
to turn it off.

It's a deliberately **focused** set (~6.5k domains): the full EasyList/
EasyPrivacy carries ~50k domains, and `setBlockedURLs` matching cost grows with
that, enough to contend for CPU. This set blocks the major ad/tracker servers
(measured: googlesyndication, doubleclick, analytics, GTM, scorecard, taboola)
while leaving legit CDNs alone. It blocks at the domain level only — no
element-hiding or path rules — so some first-party ads on a given site may slip
through.

### Finding your max quality (bitrate vs. network)

The Chromecast pulls HLS segments over your LAN; if the stream's bitrate exceeds
what the network/TV sustains, its buffer drains and playback stalls. To find the
ceiling, sweep `--video-bitrate` upward with `--stats` and watch the `tv` line:

```bash
cast --stats --stats-interval 5 --video-bitrate 8 "https://example.com"
```

Read the `tv` stats line:

- **`position +5s/5s`** (playback keeping pace with wall-clock) and state
  `PLAYING` → that bitrate is sustainable.
- **`stall ~Ns`**, **`micro-stalls ~Ns`**, or **`non-playing … (BUFFERING …)`** →
  the network can't keep up at that bitrate; back it off.

Step up (e.g. 6 → 8 → 10 → 12 Mbps) and stay at each setting a few minutes.
The receiver controls how much it buffers, so playlist length is not a reliable
countdown to a stall. For faster feedback use `--no-buffered` (shorter segments
and a four-second rolling playlist), then re-confirm your chosen bitrate with the
normal production profile. The highest setting that stays `PLAYING` with no
stalls is your ceiling; back off ~20% for headroom against network jitter.

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
- **Even-paced encoding** samples the latest frame at a constant cadence on one
  thread and feeds ffmpeg on another, with a bounded queue between them. Brief
  write stalls are absorbed without changing either media timeline. If a CFR
  video tick is lost, the old generation is stopped before any post-gap frame
  can enter it, then video and PCM are jointly re-anchored in a fresh generation.
- **Audio capture** uses a vendored [AudioTee](https://github.com/makeusabrew/audiotee) binary to tap only the cast browser's processes. An inaudible Web Audio keepalive keeps that private tap initialized while a page is silent, so media which starts later joins the existing audio stream. Your other apps are not routed through a virtual audio device.
- **Streaming** uses ffmpeg to mux H.264 + AAC into an HLS playlist served from
  a per-run temp directory (removed on exit). Restart generations use unique
  segment identities and standards-correct HLS discontinuity sequencing.
- **Casting** uses [pychromecast](https://github.com/home-assistant-libs/pychromecast) to load the HLS URL on the default media receiver.

## Troubleshooting

**No Chromecast found**  
Ensure the TV and Mac are on the same network. Try increasing `--discovery-timeout`.

**No audio on TV**  
Audio requires AudioTee. Re-run `./install.sh` (downloads a prebuilt binary or
builds one and installs it under `~/.local/share/fix-casting`), or build
manually:

```bash
cd vendor/audiotee && swift build -c release
cd ../.. && ./install.sh    # copies the build to the stable per-user path
```

The tool keeps an inaudible audio client active before tapping Chrome, so a
page can remain silent for any length of time and begin playing audio later.
It also retries autoplay automatically. If a site's player still needs user
interaction, click Play in the dedicated local Chrome window; AudioTee should
already be attached and the sound will flow into the existing cast.

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
[stats] hls     6 segments, newest segment 1.2s old
[stats] tv      PLAYING, playback position 142s
```

How to read it:

- **capture fps drops** or **capture ms rises** → Chrome tab capture is the bottleneck (CPU or page complexity)
- **behind Nx** → capture is missing its schedule and skipping ticks
- **encode fps drops** but capture is fine → ffmpeg encoding is struggling
- **frame age rises** → encoder is feeding ffmpeg stale frames (usually means capture slowed down)
- **newest segment age rises** → ffmpeg/HLS segment generation is falling behind
- **tv position** creeping further behind real time → receiver buffering or network; the playlist retains 12s by default, but actual TV holdback is client-controlled

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
