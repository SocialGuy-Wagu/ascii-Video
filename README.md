# ascii-Video

> ⚠️ **AI code.** Most of this code was vibe-coded with an Claude. Mostly works except for the ascii mp4 export option at the end.

A terminal ASCII art video player for Linux.

Play any video file directly in your terminal as live ASCII / Unicode / Braille art, with truecolor support, dithering, subtitles, and MP4 export.

## Demo

https://github.com/SocialGuy-Wagu/ascii-Video/raw/main/demo.mp4

## Features

- Streaming frame processing — no full video loaded in RAM
- Multiple render modes: `ascii`, `unicode`, `braille`, `edge`
- Color modes: truecolor (24-bit), 256-color, monochrome, colored background
- Dithering: Floyd–Steinberg, Bayer
- Brightness / contrast / gamma controls + CLAHE
- Live webcam input
- YouTube / stream URL input (via `yt-dlp`)
- Subtitle support (`.srt`, `.lrc`) with auto-discovery and offset
- Playback controls: pause, seek, mute, quit
- MP4 / GIF / image-sequence export with audio (ffmpeg pipe — no temp PNGs)
- Adaptive frame skipping + terminal resize handling
- Preset profiles: `retro`, `cinematic`, `high-detail`, `terminal-fast`, `braille`

## Requirements

- Linux
- Python 3.9+
- `ffmpeg`
- Python packages: `opencv-python`, `numpy`, `Pillow`

Optional: `yt-dlp` (URL input), `rich` (fancy UI), `tomli` (TOML config on Python < 3.11)

## Install

```sh
sudo apt install ffmpeg
pip install -r requirements.txt
```

## Usage

```sh
# Play a video
python video-ascii.py -v video.mp4

# Apply a preset
python video-ascii.py -v video.mp4 --preset cinematic

# Webcam
python video-ascii.py --webcam

# Export to MP4 (see note below — this path is flaky)
python video-ascii.py -v video.mp4 --export -o out

# Benchmark
python video-ascii.py -v video.mp4 --benchmark
```

### Controls

| Key      | Action          |
|----------|-----------------|
| `space`  | Pause / resume  |
| `←` / `→`| Seek ±10s       |
| `m`      | Toggle mute     |
| `q`      | Quit            |

## Known issues

- **MP4 export doesn't fully work.** The `--export` pipeline (frame → PIL → ffmpeg stdin) produces output but is inconsistent — frames can desync from audio, colors sometimes render wrong, and the Floyd–Steinberg dithering path is unusably slow during export. Live playback is the supported mode; treat export as experimental.

## License

MIT
