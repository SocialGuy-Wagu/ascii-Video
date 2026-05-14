# ascii-Video

A terminal ASCII art video player for Linux.

Play any video file directly in your terminal as live ASCII / Unicode / Braille art, with truecolor support, dithering, subtitles, and MP4 export.

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

# Export to MP4
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

## License

MIT
